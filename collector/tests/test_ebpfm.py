"""Unit tests for ebpfm's userspace logic.

Runs anywhere: no bcc, no root, no kernel. Everything here is either a pure
function or the process-table logic driven by synthetic events, which is where
the bugs that a live capture cannot easily reveal actually live.

    python3 -m unittest discover collector/tests
"""
import ctypes
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('EBPFM_OUTPUT_DIR', '/tmp/ebpfm_test_out')
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / 'lib'))

import ebpf_trace as T           # noqa: E402
import sockdiag                  # noqa: E402
import agent_classify as C       # noqa: E402
import slurm_args as SA          # noqa: E402

ALL_BLOCKS = set(T.BLOCK_NAMES)
# offsets a real host would get from BTF; fixed here so no bpftool is needed
FAKE_OFFS = {
    'tcp_sock': {'bytes_received': 1784, 'bytes_acked': 1840},
    'sock_common': {'skc_family': 16, 'skc_rcv_saddr': 4, 'skc_daddr': 0,
                    'skc_num': 14, 'skc_dport': 12,
                    'skc_v6_rcv_saddr': 72, 'skc_v6_daddr': 56},
}


class Ev(object):
    """A stand-in for a BCC perf event: attributes only."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

class ExitCodeConventionTest(unittest.TestCase):
    """P2-11: pin the CURRENT behaviour. No change was requested here; this test
    exists so the convention cannot be 'fixed' by accident into the 128+N shell
    form, which would silently rewrite the meaning of every exit record."""

    def test_exit_code_is_null_iff_signal_terminated(self):
        code, sig, core = T.decode_status(0)
        self.assertEqual((code, sig, core), (0, None, False))
        code, sig, core = T.decode_status(3 << 8)
        self.assertEqual((code, sig), (3, None))
        code, sig, core = T.decode_status(9)               # SIGKILL, no core
        self.assertIsNone(code)
        self.assertEqual(sig, 9)

    def test_no_128_plus_n_translation(self):
        """A signal death must NOT be reported as exit_code 137."""
        code, sig, _ = T.decode_status(9)
        self.assertNotEqual(code, 137)
        self.assertIsNone(code)


class StatusTest(unittest.TestCase):
    def test_clean_exit(self):
        self.assertEqual(T.decode_status(0), (0, None, False))
        self.assertEqual(T.decode_status(1 << 8), (1, None, False))
        self.assertEqual(T.decode_status(127 << 8), (127, None, False))

    def test_signal(self):
        self.assertEqual(T.decode_status(9), (None, 9, False))
        self.assertEqual(T.decode_status(0x80 | 11), (None, 11, True))


class ArgvTest(unittest.TestCase):
    def test_nul_separated(self):
        self.assertEqual(T.decode_argv(b'git\x00status\x00--short\x00'), 'git status --short')

    def test_empty(self):
        self.assertEqual(T.decode_argv(b''), '')
        self.assertEqual(T.decode_argv(None), '')

    def test_cap(self):
        self.assertEqual(T.decode_argv(b'a\x00' * 100, cap=5), 'a a a')


class JobIdTest(unittest.TestCase):
    def test_sbatch(self):
        self.assertEqual(T.parse_job_ids('Submitted batch job 8026641\n'), [8026641])

    def test_parsable(self):
        self.assertEqual(T.parse_job_ids('8026641;cluster\n'), [8026641])
        self.assertEqual(T.parse_job_ids('8026641\n'), [8026641])

    def test_salloc_srun(self):
        self.assertEqual(T.parse_job_ids('salloc: Granted job allocation 12345\n'), [12345])
        self.assertEqual(T.parse_job_ids('srun: job 555 queued and waiting for resources\n'), [555])

    def test_noise(self):
        self.assertEqual(T.parse_job_ids('sbatch: error: Batch job submission failed\n'), [])
        self.assertEqual(T.parse_job_ids('12\n'), [])      # too short for the bare-number rule


class IpTest(unittest.TestCase):
    def test_external(self):
        self.assertTrue(T.is_external_ip('160.79.104.10'))
        for ip in ('10.1.2.3', '127.0.0.1', 'fe80::1', '169.254.1.1', 'garbage'):
            self.assertFalse(T.is_external_ip(ip), ip)


class NullIfOffTest(unittest.TestCase):
    def test(self):
        self.assertEqual(T.null_if_off(0, 'signal', {'signal'}), 0)
        self.assertIsNone(T.null_if_off(0, 'signal', set()))


class KstackTest(unittest.TestCase):
    SAMPLE = ("[<0>] autofs_wait+0x2c1/0x800\n"
              "[<0>] autofs_mount_wait+0x8a/0x120\n"
              "[<0>] autofs_d_automount+0x11b/0x1e0\n"
              "[<0>] __traverse_mounts+0x8f/0x220\n"
              "[<0>] step_into+0x3a2/0x7a0\n"
              "[<0>] walk_component+0x6d/0x1b0\n")

    def test_frames(self):
        f = T.parse_kstack(self.SAMPLE, 5)
        self.assertEqual(f[0], 'autofs_wait')
        self.assertEqual(f[1], 'autofs_mount_wait')      # the wedge signature
        self.assertEqual(len(f), 5)

    def test_empty(self):
        self.assertIsNone(T.parse_kstack('', 5))
        self.assertIsNone(T.parse_kstack('no frames here', 5))


class DstateTest(unittest.TestCase):
    """Item 2: which source a record credits, and what it reports."""

    def test_task_wins_when_both_present(self):
        e = Ev(has_block=1, block_ns=4_200_000_000,
               has_dstate=1, dstate_ns=4_190_000_000, dstate_max=3_000_000_000, dstate_cnt=7)
        s, n, mx, src = T.pick_dstate(e, {'dstate_task', 'dstate_stat'})
        self.assertEqual(src, 'task')
        self.assertAlmostEqual(s, 4.2, places=3)
        self.assertEqual(n, 7)                 # episode count only the accumulator has
        self.assertAlmostEqual(mx, 3.0, places=3)

    def test_stat_only(self):
        e = Ev(has_block=0, block_ns=0, has_dstate=1,
               dstate_ns=1_500_000_000, dstate_max=1_000_000_000, dstate_cnt=2)
        s, n, mx, src = T.pick_dstate(e, {'dstate_stat'})
        self.assertEqual((src, n), ('stat', 2))
        self.assertAlmostEqual(s, 1.5, places=3)

    def test_zero_when_block_loaded_but_no_episode(self):
        e = Ev(has_block=0, block_ns=0, has_dstate=0, dstate_ns=0, dstate_max=0, dstate_cnt=0)
        s, n, mx, src = T.pick_dstate(e, {'dstate_stat'})
        self.assertEqual((s, n, mx), (0.0, 0, 0.0))

    def test_null_when_no_block_loaded(self):
        e = Ev(has_block=0, block_ns=0, has_dstate=0, dstate_ns=0, dstate_max=0, dstate_cnt=0)
        self.assertEqual(T.pick_dstate(e, set()), (None, None, None, None))


class ForkTotalTest(unittest.TestCase):
    def test_reads_proc_stat(self):
        v = T.read_fork_total()
        self.assertTrue(v is None or (isinstance(v, int) and v > 0))


# ---------------------------------------------------------------------------
# BPF text assembly — every block must be expressible on its own
# ---------------------------------------------------------------------------

class BpfTextTest(unittest.TestCase):
    def _t(self, feats):
        return T.build_bpf_text(set(feats), offs=FAKE_OFFS)

    def test_base(self):
        txt = self._t(set())
        for probe in ('sched_process_fork', 'sched_process_exec', 'sched_process_exit'):
            self.assertIn(probe, txt)
        for absent in ('signal->', 'inet_sock_set_state', 'sys_enter_write',
                       'sched_stat_blocked', 'kretprobe__tcp_sendmsg', 'sum_block_runtime'):
            self.assertNotIn(absent, txt)

    def test_every_block_alone_is_valid_text(self):
        for name in T.BLOCK_NAMES:
            txt = self._t({name})
            self.assertIn('sched_process_exit', txt, name)
            self.assertNotIn('__', txt.split('struct data_t')[0].replace('__INC__', ''),
                             '%s left an unreplaced placeholder' % name)

    def test_no_placeholders_left_anywhere(self):
        txt = self._t(ALL_BLOCKS)
        for ph in ('__INC__', '__CPU__', '__IO__', '__DELAY__', '__PIDS__', '__TTY__',
                   '__CGID__', '__ARGV_DEFS__', '__ARGV_EXEC__', '__SUBMIT_DEFS__',
                   '__SUBMIT_EXEC__', '__SUBMIT_EXIT__', '__TCP_COMMON__', '__TCP_TP__',
                   '__ACCEPT_PROBE__', '__DSTATE_DEFS__', '__DSTATE_READ__',
                   '__DSTATE_TASK__', '__DSTATE_DEL__', '__NETB_DEFS__', '__NETB_READ__',
                   '__TCP_BYTES__', '__NETP_SOCK_LOCAL__', '__NETP_SOCK_PEER__',
                   '__NETP_BIRTH__'):
            self.assertNotIn(ph, txt, ph)

    def test_dstate_task(self):
        txt = self._t({'dstate_task'})
        self.assertIn('task->stats.sum_block_runtime', txt)
        self.assertNotIn('sched_stat_blocked', txt)

    def test_dstate_stat_accumulates_and_bounds_the_map(self):
        txt = self._t({'dstate_stat'})
        self.assertIn('TRACEPOINT_PROBE(sched, sched_stat_blocked)', txt)
        self.assertIn('BPF_HASH(dstate_acc, u32, struct dstate_t, 65536)', txt)
        self.assertIn('dstate_acc.delete(&tid)', txt)
        # a thread's accumulator must be dropped on ITS exit, not only a leader's
        self.assertIn('dstate_acc.delete(&tid);\n        return 0;', txt)

    def test_netbytes_uses_protocol_entry_points(self):
        txt = self._t({'netbytes'})
        for fn in ('kretprobe__tcp_sendmsg', 'kretprobe__tcp_recvmsg',
                   'kretprobe__udp_sendmsg', 'kretprobe__udp_recvmsg'):
            self.assertIn(fn, txt)
        # these functions return `int`: the upper half of rax is undefined, and
        # sign-extending it as data measured a 6.7 KB download as 20 GiB
        self.assertEqual(txt.count('(s64)(s32)PT_REGS_RC(ctx)'), 4)
        self.assertNotIn('(s64)PT_REGS_RC(ctx)', txt)
        # udp_* is what makes QUIC visible; unix sockets are excluded by not
        # probing sock_sendmsg at all, so no family check is needed
        self.assertNotIn('sock_sendmsg', txt)
        self.assertIn('netb.delete(&tgid)', txt)

    def test_tcp_btf_uses_offsets_and_no_net_headers(self):
        txt = self._t({'tcp_btf'})
        self.assertIn('(void *)(skp + 1784)', txt)
        self.assertIn('(void *)(skp + 1840)', txt)
        self.assertNotIn('net/sock.h', txt)
        self.assertNotIn('linux/tcp.h', txt)

    def test_tcp_hdr_variant_uses_headers(self):
        txt = self._t({'tcp_hdr'})
        self.assertIn('#include <net/sock.h>', txt)
        self.assertIn('tp->bytes_received', txt)

    def test_tcp_basic_has_no_bytes(self):
        txt = self._t({'tcp_basic'})
        self.assertIn('inet_sock_set_state', txt)
        self.assertNotIn('has_bytes = 1', txt)

    def test_tcp_stores_the_tuple_and_connect_latency(self):
        txt = self._t({'tcp_btf'})
        # the four-tuple must be in the birth map, or userspace cannot name a
        # connection that is still open
        self.assertIn('m.sport = args->sport', txt)
        self.assertIn('__builtin_memcpy(&m.daddr_v4, args->daddr, 4)', txt)
        self.assertIn('EBPFM_TCP_ESTABLISHED', txt)
        self.assertIn('m->estab_ts = bpf_ktime_get_ns()', txt)
        # the tuple must be REFRESHED at ESTABLISHED: tcp_v4_connect sets
        # SYN_SENT before inet_hash_connect assigns the source port, so the
        # sport seen at SYN_SENT is 0 and every netlink join would miss
        # split on the `if`, not the #define, which also names the constant
        estab = txt.split('if (args->newstate == EBPFM_TCP_ESTABLISHED)')[1] \
                   .split('newstate != EBPFM_TCP_CLOSE')[0]
        self.assertIn('m->sport = args->sport', estab)
        self.assertIn('__builtin_memcpy(&m->daddr_v4, args->daddr, 4)', estab)

    def test_accept_probe_reads_sock_common_offsets(self):
        txt = self._t({'tcp_accept'})
        self.assertIn('kretprobe__inet_csk_accept', txt)
        self.assertIn('(void *)(skp + 16)', txt)        # skc_family
        self.assertIn('(void *)(skp + 14)', txt)        # skc_num
        self.assertIn('(void *)(skp + 72)', txt)        # skc_v6_rcv_saddr
        self.assertIn('m.inbound = 1', txt)

    def test_accept_without_v6_offsets(self):
        offs = {'sock_common': {k: v for k, v in FAKE_OFFS['sock_common'].items()
                                if not k.startswith('skc_v6')}}
        txt = T.build_bpf_text({'tcp_accept'}, offs=offs)
        self.assertIn('kretprobe__inet_csk_accept', txt)
        self.assertNotIn('16, (void *)(skp + 0)', txt)  # no bogus v6 read

    def test_pids_variants(self):
        self.assertIn('pids[PIDTYPE_PGID]->numbers[0].nr', self._t({'signal', 'pids_signal'}))
        self.assertIn('group_leader->pids[PIDTYPE_PGID].pid', self._t({'pids_task'}))
        self.assertIn('bpf_probe_read(&pg, sizeof(pg), &sig->pids[PIDTYPE_PGID])',
                      self._t({'signal', 'pids_kread'}))

    def test_argv_variants(self):
        self.assertIn('bpf_probe_read_user(a->buf', self._t({'argv_user'}))
        txt = self._t({'argv_kernel'})
        self.assertIn('bpf_probe_read(a->buf', txt)
        self.assertNotIn('bpf_probe_read_user', txt)

    def test_submit_matches_comm_in_kernel(self):
        txt = self._t({'submit'})
        self.assertIn('TRACEPOINT_PROBE(syscalls, sys_enter_write)', txt)
        self.assertIn("d.comm[0] == 's'", txt)
        self.assertIn('args->fd > 2', txt)              # stdio only


class PeakRssTest(unittest.TestCase):
    """peak_rss_mb was structurally always 0.0.

    The kernel runs exit_mm() -- which sets tsk->mm = NULL -- BEFORE
    sched_process_exit fires, so the old `if (mm) d.hiwater_rss_pages =
    mm->hiwater_rss;` guard never passed. Measured on 6.8: task->mm was NULL at
    9/9 exits and peak_rss_mb was 0.0 on every record. Worse than missing,
    because 0.0 is a real number a reducer will average.

    signal->maxrss lives on signal_struct, survives exit_mm(), and is in pages --
    verified against getrusage/`time -v` to the byte (309.75 MB both ways)."""

    def _t(self, feats):
        return T.build_bpf_text(set(feats), offs=FAKE_OFFS)

    def test_peak_rss_comes_off_signal_not_mm(self):
        txt = self._t(['signal'])
        self.assertIn('d.hiwater_rss_pages = task->signal->maxrss', txt)

    def test_the_dead_mm_read_is_gone(self):
        """If this reappears the field silently returns to always-zero."""
        for feats in ([], ['signal'], ['signal', 'io']):
            txt = self._t(feats)
            # match the ASSIGNMENT, not the bare name -- the comment above the
            # replacement deliberately explains the old read, and should.
            self.assertNotIn('d.hiwater_rss_pages = mm->hiwater_rss', txt)
            self.assertNotIn('struct mm_struct *mm = task->mm', txt)

    def test_no_peak_rss_read_without_the_signal_block(self):
        """Without `signal` the field must not be assigned at all, so the emit
        side can null it rather than report a zero."""
        self.assertNotIn('d.hiwater_rss_pages =', self._t([]))

    def test_emit_nulls_it_when_the_block_is_absent(self):
        """The collector's own discipline: an unavailable field is null, never 0.
        This is what the old code violated -- `threads` on the very next line has
        always been guarded this way."""
        self.assertIsNone(T.null_if_off(123.4, 'signal', set()))
        self.assertEqual(T.null_if_off(123.4, 'signal', {'signal'}), 123.4)


class SelfPathTest(unittest.TestCase):
    """ebpfm.sh must re-exec itself by ABSOLUTE path.

    $0 is whatever the caller typed. Invoked as `bash ebpfm.sh start` it is the
    bare relative "ebpfm.sh", which nohup looks up on PATH rather than cwd, so
    the node-tier child died instantly with "failed to run command" while the
    parent still printed "node tier started (pid N)". Only `./ebpfm.sh start`
    happened to work, which is why the collector's own test runs missed it."""

    def setUp(self):
        self.src = (_ROOT / 'ebpfm.sh').read_text()

    def test_self_is_resolved_absolute(self):
        self.assertIn('SELF="$(cd "$(dirname "$0")"', self.src)

    def test_node_daemon_reexecs_via_self(self):
        self.assertIn('nohup "$SELF" _node_daemon', self.src)
        self.assertNotIn('nohup "$0" _node_daemon', self.src)

    def test_start_verifies_the_child_survived(self):
        """$! is the nohup process, which exits immediately if it could not
        exec -- so without a liveness check `start` reports a pid that is
        already gone and returns 0."""
        self.assertIn('node tier FAILED to start', self.src)

    def test_bundle_extractor_does_not_read_dollar_zero(self):
        self.assertNotIn('__EBPFM_PAYLOAD__$/d\' "$0"', self.src)


class BtfWalkTest(unittest.TestCase):
    """The members tcp_accept needs sit inside ANONYMOUS unions in
    struct sock_common, so the walker has to descend into unnamed members. A flat
    scan of `members` finds only skc_family, which is how this was first missed."""

    # a cut-down mirror of the real layout: two anonymous unions wrapping
    # anonymous structs, exactly as the kernel declares sock_common
    TYPES = [
        {'id': 1, 'kind': 'INT', 'name': 'unsigned int'},
        {'id': 2, 'kind': 'TYPEDEF', 'name': '__be32', 'type_id': 1},
        {'id': 3, 'kind': 'TYPEDEF', 'name': '__be16', 'type_id': 1},
        {'id': 10, 'kind': 'STRUCT', 'name': '', 'members': [
            {'name': 'skc_daddr', 'type_id': 2, 'bits_offset': 0},
            {'name': 'skc_rcv_saddr', 'type_id': 2, 'bits_offset': 32}]},
        {'id': 11, 'kind': 'UNION', 'name': '', 'members': [
            {'name': 'skc_addrpair', 'type_id': 1, 'bits_offset': 0},
            {'name': '', 'type_id': 10, 'bits_offset': 0}]},
        {'id': 12, 'kind': 'STRUCT', 'name': '', 'members': [
            {'name': 'skc_dport', 'type_id': 3, 'bits_offset': 0},
            {'name': 'skc_num', 'type_id': 3, 'bits_offset': 16}]},
        {'id': 13, 'kind': 'UNION', 'name': '', 'members': [
            {'name': 'skc_portpair', 'type_id': 1, 'bits_offset': 0},
            {'name': '', 'type_id': 12, 'bits_offset': 0}]},
        {'id': 20, 'kind': 'STRUCT', 'name': 'sock_common', 'members': [
            {'name': '', 'type_id': 11, 'bits_offset': 0},
            {'name': 'skc_family', 'type_id': 3, 'bits_offset': 128},
            {'name': '', 'type_id': 13, 'bits_offset': 96}]},
    ]

    def test_descends_anonymous_members(self):
        by_id = {t['id']: t for t in self.TYPES}
        out = {}
        T.btf_collect(by_id, 20, {'skc_family', 'skc_daddr', 'skc_rcv_saddr',
                                  'skc_dport', 'skc_num'}, 0, out)
        self.assertEqual(out, {'skc_daddr': 0, 'skc_rcv_saddr': 4,
                               'skc_dport': 12, 'skc_num': 14, 'skc_family': 16})

    def test_accepts_either_type_key(self):
        types = [dict(t) for t in self.TYPES]
        for t in types:                     # bpftool has used both spellings
            for m in t.get('members', ()):
                if 'type_id' in m:
                    m['type'] = m.pop('type_id')
        by_id = {t['id']: t for t in types}
        out = {}
        T.btf_collect(by_id, 20, {'skc_num'}, 0, out)
        self.assertEqual(out, {'skc_num': 14})

    def test_bpftool_spells_anonymous_as_anon(self):
        """The real dump uses the literal '(anon)', not an empty name. Reading it
        as a member name is how the sock_common offsets went missing at first."""
        types = [dict(t, name=('(anon)' if t.get('name') == '' else t.get('name')),
                      members=[dict(m, name=('(anon)' if (m.get('name') or '') == '' else m['name']))
                               for m in t.get('members', ())])
                 for t in self.TYPES]
        by_id = {t['id']: t for t in types}
        out = {}
        T.btf_collect(by_id, 20, {'skc_daddr', 'skc_rcv_saddr', 'skc_dport',
                                  'skc_num', 'skc_family'}, 0, out)
        self.assertEqual(out, {'skc_daddr': 0, 'skc_rcv_saddr': 4,
                               'skc_dport': 12, 'skc_num': 14, 'skc_family': 16})

    def test_recursion_is_bounded(self):
        by_id = {1: {'id': 1, 'kind': 'STRUCT', 'name': 'loop',
                     'members': [{'name': '', 'type_id': 1, 'bits_offset': 0}]}}
        out = {}
        T.btf_collect(by_id, 1, {'nothing'}, 0, out)     # must return, not hang
        self.assertEqual(out, {})


class BlockTableTest(unittest.TestCase):
    def test_dstate_blocks_are_not_alternatives(self):
        """Both may load at once so a capture can cross-check them."""
        for nm in T.DSTATE_BLOCKS:
            self.assertNotIn(nm, T.ALTERNATIVES)

    def test_every_requirement_names_a_real_block(self):
        for name, requires, _p in T.BLOCKS:
            for r in requires:
                self.assertIn(r, T.BLOCK_NAMES, '%s requires unknown %s' % (name, r))

    def test_btf_required_blocks_are_known(self):
        for blk in T.BTF_REQUIRED:
            self.assertIn(blk, T.BLOCK_NAMES)


# ---------------------------------------------------------------------------
# process table, ancestry, attribution
# ---------------------------------------------------------------------------

class TableTest(unittest.TestCase):
    def setUp(self):
        T.proc.clear()
        T._children.clear()
        T._dropped['n'] = 0
        T._dropped['commands'] = {}
        self._min_uid = T.MIN_UID
        self._tgid = T._tgid_of
        T.MIN_UID = 0
        T._tgid_of = lambda pid: pid             # every synthetic pid is a leader
        # a claude_code root, a shell under it, a tool under that
        T.proc_add(100, 1, comm='node', args='node /x/.claude/cli.js', user='u', uid=1000,
                   cwd='/home/u/proj')
        T.proc_add(101, 100, comm='sh', args='sh -c git status', user='u', uid=1000)
        T.proc_add(102, 101, comm='git', args=None, user='u', uid=1000)
        # a human shell on a tty
        T.proc_add(200, 1, comm='bash', args='-bash', tty='pts/3', user='h', uid=1004)
        # a VS Code extension host and a helper it spawned
        T.proc_add(300, 1, comm='node', args='node /x/.vscode-server/s.js', user='v', uid=1001)
        T.proc_add(301, 300, comm='ls', args='ls', user='v', uid=1001)

    def tearDown(self):
        T.MIN_UID = self._min_uid
        T._tgid_of = self._tgid

    # --- ancestry ---------------------------------------------------------
    def test_nearest_agent(self):
        self.assertTrue(T.resolve(102))
        m = T.proc[102]
        self.assertEqual((m['agent_pid'], m['agent_type'], m['depth']), (100, 'claude_code', 2))

    def test_inherited_argv_does_not_mint_a_root(self):
        T.proc_add(103, 100, comm=None, args='node /x/.claude/cli.js',
                   argv_source='inherit', user='u', uid=1000)
        self.assertTrue(T.resolve(103))
        self.assertEqual(T.proc[103]['agent_pid'], 100)
        self.assertFalse(T.proc[103]['is_agent'])

    def test_tree_root_is_topmost_agent(self):
        T.proc_add(302, 300, comm='node', args='node /x/.vscode-server/helper.js',
                   user='v', uid=1001)
        T.proc_add(303, 302, comm='git', args='git status', user='v', uid=1001)
        self.assertTrue(T.resolve(303))
        self.assertEqual(T.proc[303]['agent_pid'], 302)   # nearest
        self.assertEqual(T.tree_root(303), 300)           # top-most
        self.assertEqual(T.tree_root(100), 100)
        self.assertIsNone(T.tree_root(200))

    def test_invalidate_on_exec(self):
        T.resolve(102)
        T.proc[101]['args'] = 'node /y/.claude/other.js'
        T._invalidate_attribution(101)
        self.assertTrue(T.resolve(102))
        self.assertEqual(T.proc[102]['agent_pid'], 101)
        self.assertEqual(T.proc[102]['depth'], 1)

    def test_children_index_follows_reparent_and_delete(self):
        self.assertEqual(T._children[101], {102})
        T.proc_set_ppid(T.proc[102], 100)
        self.assertNotIn(101, T._children)
        self.assertIn(102, T._children[100])
        T.proc_del(100)
        self.assertNotIn(100, T.proc)
        self.assertNotIn(100, T._children)

    # --- attribution (item 6) --------------------------------------------
    def test_attribution_precedence(self):
        self.assertEqual(T.actor_of(T.proc[102]), ('agent', 'ancestry'))
        self.assertEqual(T.actor_of(T.proc[200]), ('human', 'tty'))

    def test_uid_rung_is_off_by_default(self):
        T.proc_add(900, 1, comm='sleep', args='sleep 300', user='h', uid=1004)
        self.assertEqual(T.actor_of(T.proc[900]), (None, None))

    def test_uid_rung_when_enabled(self):
        T.MIN_UID = 1000
        T.proc_add(900, 1, comm='sleep', args='sleep 300', user='h', uid=1004)
        self.assertEqual(T.actor_of(T.proc[900]), ('human', 'uid'))

    def test_uid_rung_still_ignores_system_uids(self):
        T.MIN_UID = 1000
        T.proc_add(901, 1, comm='cron', args='cron', user='root', uid=0)
        self.assertEqual(T.actor_of(T.proc[901]), (None, None))

    def test_ancestry_beats_uid_when_both_apply(self):
        T.MIN_UID = 1000
        self.assertEqual(T.actor_of(T.proc[102]), ('agent', 'ancestry'))

    def test_actor3_keeps_the_three_physical_classes(self):
        seen = set()
        for pid in (102, 200, 301):
            m = T.proc[pid]
            actor, attr = T.actor_of(m)
            seen.add(T._base_identity(m, actor, attr)['actor3'])
        self.assertEqual(seen, {'agent', 'human', 'human-vscode'})

    def test_vscode_helper_is_human_vscode_not_agent(self):
        self.assertTrue(T.resolve(301))
        ident = T._base_identity(T.proc[301], 'agent', 'ancestry')
        self.assertEqual(ident['actor3'], 'human-vscode')
        self.assertEqual(ident['session_key'], 'apid:300')

    # --- cwd (item 1) -----------------------------------------------------
    def test_cwd_inherits_from_parent(self):
        T.proc_add(104, 100, comm='grep', args='grep x', user='u', uid=1000)
        T._inherit_cwd(T.proc[104])
        self.assertEqual(T.proc[104]['cwd'], '/home/u/proj')

    def test_cwd_inherit_does_not_overwrite_a_read(self):
        T.proc_add(105, 100, comm='git', args='git log', user='u', uid=1000,
                   cwd='/tmp/elsewhere')
        T._inherit_cwd(T.proc[105])
        self.assertEqual(T.proc[105]['cwd'], '/tmp/elsewhere')

    def test_cwd_absent_when_parent_has_none(self):
        T.proc_add(201, 200, comm='ls', args='ls', user='h', uid=1004)
        T._inherit_cwd(T.proc[201])
        self.assertIsNone(T.proc[201]['cwd'])

    def test_identity_carries_cwd_and_not_its_source(self):
        """Whether cwd was read or inherited is not worth a field on every record
        once the value itself is there."""
        ident = T._base_identity(T.proc[100], 'agent', 'ancestry')
        self.assertEqual(ident['cwd'], '/home/u/proj')
        self.assertNotIn('cwd_source', ident)

    # --- fork handling ----------------------------------------------------
    def test_thread_clone_is_ignored(self):
        T._tgid_of = lambda pid: 100                 # /proc says Tgid is the parent
        T.on_fork(Ev(pid=9001, ppid=100, uid=1000))
        self.assertNotIn(9001, T.proc)
        T._tgid_of = lambda pid: None                # already gone
        T.on_fork(Ev(pid=9002, ppid=100, uid=1000))
        self.assertNotIn(9002, T.proc)

    def test_fork_inherits_identity_and_cwd(self):
        T.on_fork(Ev(pid=9003, ppid=100, uid=1000))
        m = T.proc[9003]
        self.assertEqual(m['cwd'], '/home/u/proj')
        self.assertEqual(m['argv_source'], 'inherit')

    def test_pid_recycle_resets_the_entry(self):
        T.proc[102]['exited'] = True
        T.proc[102]['args'] = 'stale'
        T.on_fork(Ev(pid=102, ppid=200, uid=1004))
        m = T.proc[102]
        self.assertFalse(m['exited'])
        self.assertEqual(m['ppid'], 200)
        self.assertEqual(m['args'], '-bash')         # from the NEW parent
        self.assertEqual(m['tty'], 'pts/3')

    def test_tty_is_not_inherited_over_an_authoritative_reading(self):
        """A setsid'd child of a tty shell has NO tty. Inheriting the parent's
        would relabel a detached process as an interactive human and hide it from
        the uid rung entirely."""
        T.proc_add(210, 200, comm='sleep', args='sleep 300', user='h', uid=1004)
        T._inherit_tty(T.proc[210], authoritative=True)
        self.assertIsNone(T.proc[210]['tty'])
        self.assertEqual(T.actor_of(T.proc[210]), (None, None))

    def test_tty_is_inherited_when_we_have_no_reading(self):
        """A fork-without-exec child does inherit the parent's terminal, so with
        no reading of its own, copying is the right guess."""
        T.proc_add(211, 200, comm='sh', args='-bash', user='h', uid=1004)
        T._inherit_tty(T.proc[211], authoritative=False)
        self.assertEqual(T.proc[211]['tty'], 'pts/3')
        self.assertEqual(T.actor_of(T.proc[211]), ('human', 'tty'))

    def test_detached_process_reaches_the_uid_rung(self):
        T.MIN_UID = 1000
        T.proc_add(212, 1, comm='sleep', args='sleep 300', user='h', uid=1004)
        T._inherit_tty(T.proc[212], authoritative=True)
        self.assertEqual(T.actor_of(T.proc[212]), ('human', 'uid'))

    def test_dropped_bucket_counts(self):
        T.proc_add(902, 1, comm='systemd-udevd', args='udevd', user='root', uid=0)
        actor, _ = T.actor_of(T.proc[902])
        self.assertIsNone(actor)
        T._note_dropped(T.proc[902])
        self.assertEqual(T._dropped['n'], 1)
        self.assertEqual(T._dropped['commands']['systemd-udevd'], 1)


# ---------------------------------------------------------------------------
# netlink socket-diag parser (item 3a)
# ---------------------------------------------------------------------------

def _mk_tcp_info(rtt_us=31200, retrans=0, acked=219004, received=41882133,
                 last_sent=4100, last_recv=120, segs_out=900, segs_in=1200):
    blob = bytearray(160)
    blob[2] = retrans & 0xff
    struct.pack_into('=I', blob, 44, last_sent)
    struct.pack_into('=I', blob, 52, last_recv)
    struct.pack_into('=I', blob, 68, rtt_us)
    struct.pack_into('=I', blob, 100, retrans)
    struct.pack_into('=Q', blob, 120, acked)
    struct.pack_into('=Q', blob, 128, received)
    struct.pack_into('=I', blob, 136, segs_out)
    struct.pack_into('=I', blob, 140, segs_in)
    return bytes(blob)


def _mk_msg(saddr='10.243.49.205', sport=45964, daddr='140.82.114.6', dport=443,
            state=1, uid=1004, inode=12345, info=None, family=socket.AF_INET):
    sockid = struct.pack('!HH', sport, dport)
    if family == socket.AF_INET:
        sockid += socket.inet_pton(socket.AF_INET, saddr) + b'\x00' * 12
        sockid += socket.inet_pton(socket.AF_INET, daddr) + b'\x00' * 12
    else:
        sockid += socket.inet_pton(socket.AF_INET6, saddr)
        sockid += socket.inet_pton(socket.AF_INET6, daddr)
    sockid += struct.pack('=I', 0) + struct.pack('=II', 0, 0)
    body = struct.pack('=BBBB', family, state, 0, 0) + sockid
    body += struct.pack('=IIIII', 0, 0, 0, uid, inode)
    attrs = b''
    if info is not None:
        pad = (-len(info)) % 4
        attrs = struct.pack('=HH', 4 + len(info), sockdiag.INET_DIAG_INFO) + info + b'\x00' * pad
    payload = body + attrs
    hdr = struct.pack('=IHHII', 16 + len(payload), sockdiag.SOCK_DIAG_BY_FAMILY, 2, 1, 0)
    return hdr + payload


class EnvelopeTest(unittest.TestCase):
    """The record envelope and the identity key set — structural invariants that
    must survive every future schema bump."""

    def test_envelope_carries_ts_epoch(self):
        """No `source`/`collector`: constants ('ebpf', 'ebpf_marthen_new') on every
        record said nothing a reader did not already know from the feed it read,
        and cost width on each line. Pre-7 captures still carry them."""
        e = T._envelope('exit')
        self.assertEqual(set(e), {'ts', 'ts_epoch', 'host', 'event', 'collector_pid',
                                  'schema_version'})
        self.assertIsInstance(e['ts_epoch'], float)
        self.assertEqual(e['event'], 'exit')

    def test_envelope_names_the_collector_process(self):
        e = T._envelope('exit')
        self.assertEqual(e['collector_pid'], os.getpid())

    def test_ts_and_ts_epoch_agree(self):
        """ts_epoch is the authoritative binning key; it must describe the same
        instant as the human-readable ts, not drift from it."""
        e = T._envelope('exit')
        parsed = datetime.strptime(e['ts'], '%Y-%m-%d %H:%M:%S')
        self.assertLess(abs(parsed.timestamp() - e['ts_epoch']), 2.0)

    def test_stub_identity_key_set_equals_base_identity(self):
        """The guard for the defect this replaced: three call sites hand-built a
        7-key subset against _base_identity's 21, so tcp/conn records silently
        omitted fourteen keys. Equality here is what stops that recurring."""
        base = T._base_identity({'pid': 1, 'ppid': 0}, 'human', 'tty')
        self.assertEqual(set(base), set(T._stub_identity(5, 1000, 'x')))
        self.assertEqual(len(T._IDENTITY_KEYS), len(base))

    def test_stub_identity_nulls_everything_it_does_not_know(self):
        st = T._stub_identity(5, 1000, 'curl')
        self.assertEqual((st['pid'], st['uid'], st['command']), (5, 1000, 'curl'))
        self.assertIsNone(st['actor'])
        self.assertIsNone(st['agent_pid'])
        self.assertIsNone(st['cgroup'])
        # present-and-null, never absent
        for k in ('ppid', 'tty', 'args', 'cwd', 'session_key', 'tree_root_pid'):
            self.assertIn(k, st)
            self.assertIsNone(st[k])

    def test_schema_version_is_pinned(self):
        """Never asserted before. A bump is a deliberate act with a downstream
        cost, not something that should drift in unnoticed. 7 = net_endpoints,
        the netio series, IPv6 UDP in the net_* totals, proto on conn, and the
        envelope's source/collector dropped for collector_pid."""
        self.assertEqual(T.SCHEMA_VERSION, 7)

    def test_top_n(self):
        self.assertEqual(T._top_n({'a': 3, 'b': 9, 'c': 1}, 2), {'b': 9, 'a': 3})
        self.assertEqual(T._top_n({}, 5), {})
        self.assertIsNone(T._top_n({}, 5, or_none=True))


# ---------------------------------------------------------------------------
# schema 6: per-connection records folded onto the process
# ---------------------------------------------------------------------------

class FakeWriter(object):
    """Stands in for DailyWriter. Records only; no file, no day rollover."""

    def __init__(self):
        self.recs = []
        self.n = 0

    def write(self, rec):
        self.recs.append(rec)
        self.n += 1

    def of(self, event):
        return [r for r in self.recs if r['event'] == event]


def _tcp_ev(pid=101, daddr='104.18.0.1', dport=443, kind=None, established=1,
            has_bytes=1, rx=1000, tx=200, dur_ns=int(2e9), connect_ns=int(30e6),
            inbound=0, uid=1000, comm=b'node'):
    return Ev(pid=pid, uid=uid, comm=comm, family=2,
              saddr_v4=int.from_bytes(socket.inet_aton('10.0.0.2'), 'little'),
              daddr_v4=int.from_bytes(socket.inet_aton(daddr), 'little'),
              saddr_v6=0, daddr_v6=0, sport=40000, dport=dport,
              kind=(T.TCP_KIND_ACCEPT if kind == 'accept' else T.TCP_KIND_CLOSE),
              established=established, has_bytes=has_bytes, rx_bytes=rx,
              tx_bytes=tx, dur_ns=dur_ns, connect_ns=connect_ns, inbound=inbound)


def _exit_ev(pid=101, ppid=100, uid=1000, comm=b'node', cpu_ns=int(1e9),
             exit_code=0, run_ns=int(1e9), net_tx=0, net_rx=0, net_calls=0):
    """A full exit event. Every field on_exit reads eagerly must be present:
    null_if_off() takes an already-evaluated argument, so a missing attribute is
    an AttributeError even when the block is off. `has_net` follows the kernel:
    set exactly when the netb map had an entry for the process."""
    return Ev(pid=pid, ppid=ppid, uid=uid, comm=comm, exit_code=exit_code,
              ktime_ns=int(5e9), start_ns=int(1e9),
              utime_ns=cpu_ns, stime_ns=0, cutime_ns=0, cstime_ns=0,
              hiwater_rss_pages=100, nr_threads=1, min_flt=10, maj_flt=0,
              cmaj_flt=0, nvcsw=1, nivcsw=0, run_ns=run_ns, wait_ns=0,
              blkio_ns=0, swapin_ns=0, freepages_ns=0,
              rd_bytes=0, wr_bytes=0, rchar=0, wchar=0,
              net_tx=net_tx, net_rx=net_rx, net_calls=net_calls,
              has_net=1 if (net_tx or net_rx or net_calls) else 0,
              pgrp=pid, sid=pid, has_tty=0, tty=b'', cgid=0,
              has_block=0, block_ns=0, has_dacc=0)


class ConnFoldTest(unittest.TestCase):
    """The schema-6 change: `tcp` and `accept` records no longer exist and the
    connection reaches the feed inside its process's exit record."""

    def setUp(self):
        T.proc.clear(); T._children.clear()
        T._pending_exits.clear()
        T._conn_dropped['n'] = 0
        T._conn_dropped['commands'] = {}
        self._saved = (T.writer, T.FEATURES, T._tgid_of, T.MIN_UID,
                       T.MIN_DURATION, T.MIN_CPU, T.EXIT_HOLD_MS, T.TCP_ALL)
        T.writer = self.W = FakeWriter()
        T.FEATURES = set()
        T._tgid_of = lambda pid: pid
        T.MIN_UID = 0
        T.MIN_DURATION = 0.0
        T.MIN_CPU = 0.0
        T.EXIT_HOLD_MS = 250
        T.proc_add(100, 1, comm='node', args='node /x/.claude/cli.js',
                   user='u', uid=1000)
        T.proc_add(101, 100, comm='curl', args='curl https://api.example', user='u', uid=1000)

    def tearDown(self):
        (T.writer, T.FEATURES, T._tgid_of, T.MIN_UID,
         T.MIN_DURATION, T.MIN_CPU, T.EXIT_HOLD_MS, T.TCP_ALL) = self._saved
        T._pending_exits.clear()

    # --- the records are gone ---------------------------------------------
    def test_on_tcp_writes_nothing(self):
        T.on_tcp(_tcp_ev())
        self.assertEqual(self.W.recs, [])
        self.assertEqual(T.proc[101]['conns']['n'], 1)

    def test_no_tcp_or_accept_records_survive_a_full_cycle(self):
        T.on_tcp(_tcp_ev())
        T.on_tcp(_tcp_ev(kind='accept', inbound=1))
        T.on_exit(_exit_ev())
        T.drain_exits(final=True)
        self.assertEqual(self.W.of('tcp'), [])
        self.assertEqual(self.W.of('accept'), [])
        self.assertEqual(len(self.W.of('exit')), 1)

    # --- THE ordering regression ------------------------------------------
    def test_socket_close_after_the_exit_event_still_lands_in_the_record(self):
        """do_exit() runs trace_sched_process_exit() BEFORE exit_files(), so a
        process's own TCP_CLOSE events arrive AFTER its exit event. If the exit
        record were written inline this connection would be lost -- which is
        every connection a process was still holding when it exited."""
        T.on_exit(_exit_ev())
        self.assertEqual(self.W.recs, [], 'exit record must not be written inline')
        T.on_tcp(_tcp_ev())                      # arrives late, as the kernel does it
        T.drain_exits(final=True)
        rec = self.W.of('exit')[0]
        self.assertEqual(rec['conns']['n'], 1)
        self.assertEqual(rec['conns']['established'], 1)

    def test_hold_is_respected_until_it_elapses(self):
        T.on_exit(_exit_ev())
        self.assertEqual(T.drain_exits(), 0)     # not due yet
        self.assertEqual(self.W.recs, [])
        T._pending_exits[0] = (0.0,) + tuple(T._pending_exits[0][1:])
        self.assertEqual(T.drain_exits(), 1)
        self.assertEqual(len(self.W.of('exit')), 1)

    def test_zero_hold_restores_the_inline_emit(self):
        T.EXIT_HOLD_MS = 0
        T.on_exit(_exit_ev())
        self.assertEqual(len(self.W.of('exit')), 1)
        self.assertEqual(len(T._pending_exits), 0)

    # --- the block itself --------------------------------------------------
    def test_repeat_calls_to_one_endpoint_collapse_into_one_peer(self):
        """The point of folding: twenty requests to one API host were twenty
        records and are now one peer carrying n=20."""
        for _ in range(20):
            T.on_tcp(_tcp_ev(daddr='104.18.0.1'))
        T.on_tcp(_tcp_ev(daddr='140.82.112.3'))
        c = T._conns_block(T.proc[101])
        self.assertEqual(c['n'], 21)
        self.assertEqual(c['peers_total'], 2)
        top = [p for p in c['peers'] if p['daddr'] == '104.18.0.1'][0]
        self.assertEqual(top['n'], 20)
        self.assertEqual(top['rx_bytes'], 20000)

    def test_totals_and_failed_connects(self):
        T.on_tcp(_tcp_ev(rx=1000, tx=200))
        T.on_tcp(_tcp_ev(established=0, has_bytes=0, connect_ns=0))
        c = T._conns_block(T.proc[101])
        self.assertEqual((c['n'], c['established'], c['failed']), (2, 1, 1))
        self.assertEqual((c['rx_bytes'], c['tx_bytes']), (1000, 200))
        self.assertEqual(c['external'], 2)

    def test_accept_counts_as_inbound_and_established_not_failed(self):
        T.on_tcp(_tcp_ev(kind='accept', inbound=1, established=0))
        c = T._conns_block(T.proc[101])
        self.assertEqual((c['accepted'], c['established'], c['failed']), (1, 1, 0))

    def test_bytes_are_null_not_zero_when_the_block_did_not_load(self):
        """Design principle 4: a dropped block emits null, never 0. has_bytes=0
        is the tcp_basic variant, which cannot read byte counters at all."""
        T.on_tcp(_tcp_ev(has_bytes=0))
        c = T._conns_block(T.proc[101])
        self.assertIsNone(c['rx_bytes'])
        self.assertIsNone(c['tx_bytes'])
        self.assertEqual(c['n'], 1)

    def test_no_connections_is_none_not_an_empty_block(self):
        self.assertIsNone(T._conns_block(T.proc[101]))
        T.on_exit(_exit_ev())
        T.drain_exits(final=True)
        self.assertIsNone(self.W.of('exit')[0]['conns'])

    def test_peer_map_is_capped_and_the_overflow_is_counted(self):
        for i in range(T._PEER_KEYS_MAX + 5):
            T.on_tcp(_tcp_ev(daddr='104.18.%d.%d' % (i // 256, i % 256)))
        c = T._conns_block(T.proc[101])
        self.assertEqual(c['peers_total'], T._PEER_KEYS_MAX)
        self.assertEqual(c['peers_overflow'], 5)
        self.assertEqual(c['n'], T._PEER_KEYS_MAX + 5)
        self.assertEqual(len(c['peers']), T.CONNS_TOPN)

    def test_providers_are_counted(self):
        T.proc[101]['conns'] = None
        T._conn_fold(T.proc[101], daddr='1.2.3.4', dport=443, external=True,
                     provider='anthropic', inbound=False, established=True)
        T._conn_fold(T.proc[101], daddr='1.2.3.5', dport=443, external=True,
                     provider='anthropic', inbound=False, established=True)
        T._conn_fold(T.proc[101], daddr='1.2.3.6', dport=443, external=True,
                     provider='github', inbound=False, established=True)
        c = T._conns_block(T.proc[101])
        self.assertEqual(c['providers'], {'anthropic': 2, 'github': 1})

    # --- the quiet-process filters ----------------------------------------
    def test_a_short_process_that_made_a_connection_is_kept(self):
        """MIN_DURATION would discard this curl, and with it the connection that
        made it worth keeping. The filter is decided at exit and applied at
        drain, so the late socket close rescues the record."""
        T.MIN_DURATION = 10.0
        T.on_exit(_exit_ev(run_ns=int(1e6)))
        T.on_tcp(_tcp_ev())
        T.drain_exits(final=True)
        self.assertEqual(len(self.W.of('exit')), 1)
        self.assertEqual(self.W.of('exit')[0]['conns']['n'], 1)

    def test_a_short_process_with_no_connection_is_still_dropped(self):
        T.MIN_DURATION = 10.0
        T.on_exit(_exit_ev(run_ns=int(1e6)))
        T.drain_exits(final=True)
        self.assertEqual(self.W.of('exit'), [])

    # --- loss accounting ---------------------------------------------------
    def test_connections_on_a_never_emitted_process_are_counted_at_gc(self):
        """A dropped exit event (perf_lost) leaves a process that folded
        connections and never produced a record. They are lost -- that is
        unavoidable -- but `stop.conns_dropped` has to say so, because with the
        standalone records gone this is the only way the loss is visible."""
        T.on_tcp(_tcp_ev(pid=101))
        T.proc[101]['exited'] = True             # exit event never arrived
        T.proc[101]['gc_ts'] = 0.0
        T.gc()
        self.assertEqual(T._conn_dropped['n'], 1)
        self.assertEqual(T._conn_dropped['commands'], {'curl': 1})
        self.assertNotIn(101, T.proc)

    def test_an_emitted_process_is_not_counted_as_dropped(self):
        T.on_tcp(_tcp_ev())
        T.on_exit(_exit_ev())
        T.drain_exits(final=True)
        T.proc[101]['gc_ts'] = 0.0
        T.gc()
        self.assertEqual(T._conn_dropped['n'], 0)

    def test_untracked_pid_is_ignored_unless_tcp_all(self):
        T.on_tcp(_tcp_ev(pid=777))
        self.assertNotIn(777, T.proc)
        T.TCP_ALL = True
        T.on_tcp(_tcp_ev(pid=777))
        self.assertEqual(T.proc[777]['conns']['n'], 1)


class FlushPolicyTest(unittest.TestCase):
    """DailyWriter's write(2) policy. Through schema 5 every record was followed
    by a flush, so the syscall rate WAS the event rate -- unbounded, and highest
    exactly when the node is busiest. Under 'poll' the loop flushes once per
    round instead, which caps syscalls at the poll rate however many records the
    round produced."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ebpfm_flush_')
        self._mode = T.FLUSH_MODE
        self.day = datetime.now().strftime('%Y-%m-%d')

    def tearDown(self):
        T.FLUSH_MODE = self._mode
        shutil.rmtree(self.dir, ignore_errors=True)

    def _path(self, stream='exits'):
        return os.path.join(self.dir, self.day, 'node0.%s.jsonl' % stream)

    def _on_disk(self, stream='exits'):
        """What a reader would see RIGHT NOW -- the point of the whole policy.
        Defaults to the exits stream because _rec() defaults to an exit."""
        try:
            with open(self._path(stream)) as f:
                return [json.loads(l) for l in f if l.strip()]
        except IOError:
            return []

    def _rec(self, event='exit'):
        return {'event': event, 'ts': 'x', 'host': 'n0'}

    # --- poll mode ---------------------------------------------------------
    def test_poll_mode_defers_the_write_until_flush(self):
        T.FLUSH_MODE = 'poll'
        w = T.DailyWriter(self.dir, 'node0')
        for _ in range(50):
            w.write(self._rec())
        self.assertEqual(w.flushes, 0)
        self.assertEqual(self._on_disk(), [], 'nothing should have reached disk yet')
        w.flush()
        self.assertEqual(w.flushes, 1, '50 records must cost ONE write(2)')
        self.assertEqual(len(self._on_disk()), 50)
        w.close()

    def test_flush_is_a_noop_when_the_round_wrote_nothing(self):
        """An idle node polls ten times a second forever; those rounds must not
        each cost a syscall."""
        T.FLUSH_MODE = 'poll'
        w = T.DailyWriter(self.dir, 'node0')
        for _ in range(100):
            w.flush()
        self.assertEqual(w.flushes, 0)
        w.write(self._rec())
        w.flush()
        w.flush()
        w.flush()
        self.assertEqual(w.flushes, 1, 'only the round with a record flushes')
        w.close()

    def test_close_flushes_what_is_pending(self):
        """The stop path writes the stop record and closes. Nothing may be lost
        between the two -- and since the split sends the exit and the stop to
        DIFFERENT files, close() has to land both, not just the last one
        touched."""
        T.FLUSH_MODE = 'poll'
        w = T.DailyWriter(self.dir, 'node0')
        w.write(self._rec())
        w.write(self._rec('stop'))
        w.close()
        self.assertEqual([r['event'] for r in self._on_disk('exits')], ['exit'])
        self.assertEqual([r['event'] for r in self._on_disk('snapshot')], ['stop'])

    def test_a_round_touching_both_streams_costs_two_writes(self):
        """The split's only I/O cost, stated as a number: a round that wrote to
        both files flushes both. It does not cost more than that however many
        records each held."""
        T.FLUSH_MODE = 'poll'
        w = T.DailyWriter(self.dir, 'node0')
        for _ in range(100):
            w.write(self._rec('exit'))
            w.write(self._rec('residency'))
        w.flush()
        self.assertEqual(w.flushes, 2, '200 records across two streams = two write(2)')
        w.close()

    def test_a_pure_exit_round_still_costs_one_write(self):
        """The split must not tax the common case: a round that never touched
        the snapshot stream does not flush it."""
        T.FLUSH_MODE = 'poll'
        w = T.DailyWriter(self.dir, 'node0')
        for _ in range(100):
            w.write(self._rec('exit'))
        w.flush()
        self.assertEqual(w.flushes, 1)
        w.close()

    def test_records_survive_a_reopen_byte_for_byte(self):
        T.FLUSH_MODE = 'poll'
        w = T.DailyWriter(self.dir, 'node0')
        for i in range(200):
            w.write({'event': 'exit', 'pid': i, 'ts': 'x'})
        w.close()
        recs = self._on_disk()
        self.assertEqual(len(recs), 200)
        self.assertEqual([r['pid'] for r in recs], list(range(200)))

    # --- record mode is still available ------------------------------------
    def test_record_mode_flushes_every_record(self):
        T.FLUSH_MODE = 'record'
        w = T.DailyWriter(self.dir, 'node0')
        for _ in range(50):
            w.write(self._rec())
        self.assertEqual(w.flushes, 50)
        self.assertEqual(len(self._on_disk()), 50, 'record mode is durable per record')
        w.close()

    def test_mode_can_be_forced_per_writer(self):
        T.FLUSH_MODE = 'poll'
        w = T.DailyWriter(self.dir, 'node0', flush_mode='record')
        w.write(self._rec())
        self.assertEqual(w.flushes, 1)
        w.close()

    # --- the amplification claim, as a test --------------------------------
    def test_poll_mode_decouples_syscalls_from_record_rate(self):
        """The property the change exists for: doubling the records in a round
        does not add a single write(2)."""
        T.FLUSH_MODE = 'poll'
        for n in (10, 100, 1000):
            w = T.DailyWriter(self.dir, 'node%d' % n)
            for _ in range(n):
                w.write(self._rec())
            w.flush()
            self.assertEqual(w.flushes, 1, '%d records still cost one write(2)' % n)
            w.close()

    def test_n_counts_records_not_flushes(self):
        T.FLUSH_MODE = 'poll'
        w = T.DailyWriter(self.dir, 'node0')
        for _ in range(7):
            w.write(self._rec())
        w.flush()
        self.assertEqual((w.n, w.flushes), (7, 1))
        w.close()


class StreamSplitTest(unittest.TestCase):
    """The day's output is two files, not one: `<host>.exits.jsonl` carries the
    per-process terminal records and `<host>.snapshot.jsonl` carries everything
    else. The split is a schema-level promise to consumers, so what matters here
    is that the mapping is TOTAL (every documented event kind has a home), that
    the two streams never bleed into each other, and that a reader can tell a
    quiet node from a dead collector."""

    # The v6 event vocabulary, as a literal rather than derived from the code:
    # adding an event kind without deciding where it belongs must fail HERE,
    # loudly, instead of defaulting into the snapshot file unnoticed.
    ALL_EVENTS = ('meta', 'exit', 'truncated', 'conn', 'netio', 'submit',
                  'residency', 'residency_totals', 'stop')
    # Retired in v6 (folded into `conns` on the owning exit record) but still
    # present in archived v4/v5 captures, which the dashboard is expected to be
    # able to read. They are connection observations, so they belong with the
    # snapshot stream -- asserted below rather than left to chance.
    RETIRED_EVENTS = ('tcp', 'accept')

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ebpfm_split_')
        self._mode = T.FLUSH_MODE
        T.FLUSH_MODE = 'poll'
        self.day = datetime.now().strftime('%Y-%m-%d')

    def tearDown(self):
        T.FLUSH_MODE = self._mode
        shutil.rmtree(self.dir, ignore_errors=True)

    def _read(self, stream, host='node0'):
        p = os.path.join(self.dir, self.day, '%s.%s.jsonl' % (host, stream))
        with open(p) as f:
            return [json.loads(l) for l in f if l.strip()]

    # --- the mapping -------------------------------------------------------
    def test_every_documented_event_maps_to_a_real_stream(self):
        for ev in self.ALL_EVENTS:
            self.assertIn(T.stream_of(ev), T.STREAMS, '%s has no stream' % ev)

    def test_only_exit_and_truncated_are_exits(self):
        """Pins the membership both ways. `truncated` belongs with `exit`
        because it IS an exit record -- one for a process whose argv the kernel
        clamped -- and normalize.py already treats the pair as one group."""
        self.assertEqual({e for e in self.ALL_EVENTS if T.stream_of(e) == 'exits'},
                         {'exit', 'truncated'})

    def test_retired_v4_v5_kinds_still_route_to_snapshot(self):
        """Archived captures still carry these; they must not land in the exits
        file, where a consumer counting process terminations would find them."""
        for ev in self.RETIRED_EVENTS:
            self.assertEqual(T.stream_of(ev), 'snapshot')

    def test_an_unknown_event_lands_in_snapshot_rather_than_nowhere(self):
        """Totality, stated. A future event kind must not be dropped or raise
        in the middle of a capture."""
        self.assertEqual(T.stream_of('some_future_event'), 'snapshot')
        self.assertEqual(T.stream_of(None), 'snapshot')

    # --- routing on disk ---------------------------------------------------
    def test_records_land_in_the_file_their_event_selects(self):
        w = T.DailyWriter(self.dir, 'node0')
        for ev in self.ALL_EVENTS:
            w.write({'event': ev, 'ts': 'x'})
        w.close()
        self.assertEqual([r['event'] for r in self._read('exits')],
                         ['exit', 'truncated'])
        self.assertEqual([r['event'] for r in self._read('snapshot')],
                         ['meta', 'conn', 'netio', 'submit',
                          'residency', 'residency_totals', 'stop'])

    def test_the_streams_do_not_bleed(self):
        w = T.DailyWriter(self.dir, 'node0')
        for ev in self.ALL_EVENTS * 20:
            w.write({'event': ev, 'ts': 'x'})
        w.close()
        self.assertTrue(all(T.stream_of(r['event']) == 'exits'
                            for r in self._read('exits')))
        self.assertTrue(all(T.stream_of(r['event']) == 'snapshot'
                            for r in self._read('snapshot')))

    def test_no_record_is_lost_across_the_split(self):
        """The count that matters: two files must still hold every record the
        one file held."""
        w = T.DailyWriter(self.dir, 'node0')
        for ev in self.ALL_EVENTS * 13:
            w.write({'event': ev, 'ts': 'x'})
        w.close()
        self.assertEqual(len(self._read('exits')) + len(self._read('snapshot')),
                         len(self.ALL_EVENTS) * 13)
        self.assertEqual(w.n, len(self.ALL_EVENTS) * 13)

    def test_ordering_is_preserved_within_a_stream(self):
        w = T.DailyWriter(self.dir, 'node0')
        for i in range(200):
            w.write({'event': 'exit', 'pid': i, 'ts': 'x'})
            w.write({'event': 'residency', 'seq': i, 'ts': 'x'})
        w.close()
        self.assertEqual([r['pid'] for r in self._read('exits')], list(range(200)))
        self.assertEqual([r['seq'] for r in self._read('snapshot')], list(range(200)))

    # --- what an operator sees --------------------------------------------
    def test_both_files_exist_once_the_day_opens(self):
        """A quiet node must be distinguishable from a collector that never
        started, so the pair is created on the first write of the day even when
        one stream stays empty."""
        w = T.DailyWriter(self.dir, 'node0')
        w.write({'event': 'meta', 'ts': 'x'})       # startup record only
        w.close()
        self.assertEqual(self._read('exits'), [], 'no exits observed, not a missing file')
        self.assertEqual(len(self._read('snapshot')), 1)

    def test_ts_epoch_backstop_still_applies_to_both_streams(self):
        w = T.DailyWriter(self.dir, 'node0')
        w.write({'event': 'exit', 'ts': 'x'})
        w.write({'event': 'residency', 'ts': 'x'})
        w.close()
        for s in T.STREAMS:
            for r in self._read(s):
                self.assertIn('ts_epoch', r)

    def test_path_names_the_file_without_writing_it(self):
        w = T.DailyWriter(self.dir, 'node0')
        w.write({'event': 'exit', 'ts': 'x'})
        self.assertEqual(w.path('exits'),
                         os.path.join(self.dir, self.day, 'node0.exits.jsonl'))
        self.assertEqual(w.path('snapshot'),
                         os.path.join(self.dir, self.day, 'node0.snapshot.jsonl'))
        w.close()

    # --- day rollover ------------------------------------------------------
    def test_rollover_opens_a_new_pair_and_strands_nothing(self):
        """The rollover path closes two handles, not one. Records buffered in
        EITHER stream must reach disk before the day changes -- with one handle
        that was automatic, with two it is a thing that can be got wrong.

        _roll() is driven directly because the day is taken from the wall clock:
        reassigning w.day would still resolve to today's directory and prove
        nothing."""
        w = T.DailyWriter(self.dir, 'node0')
        w.write({'event': 'exit', 'ts': 'x'})
        w.write({'event': 'residency', 'ts': 'x'})
        self.assertEqual(self._read('exits'), [], 'still buffered pre-roll')

        w._roll('1999-01-01')                       # the midnight transition
        self.assertEqual(w.flushes, 2, 'the roll flushed both streams')
        self.assertEqual(len(self._read('exits')), 1, 'day 1 exit reached disk')
        self.assertEqual(len(self._read('snapshot')), 1, 'day 1 residency reached disk')

        day2 = os.path.join(self.dir, '1999-01-01')  # the new day opens a PAIR
        self.assertEqual(sorted(os.listdir(day2)),
                         ['node0.exits.jsonl', 'node0.snapshot.jsonl'])
        self.assertEqual([os.path.getsize(os.path.join(day2, f))
                          for f in sorted(os.listdir(day2))], [0, 0])
        w.close()

    def test_record_mode_routes_the_same_way(self):
        w = T.DailyWriter(self.dir, 'node0', flush_mode='record')
        w.write({'event': 'exit', 'ts': 'x'})
        w.write({'event': 'residency', 'ts': 'x'})
        self.assertEqual(w.flushes, 2, 'record mode is durable per record')
        self.assertEqual(len(self._read('exits')), 1)
        self.assertEqual(len(self._read('snapshot')), 1)
        w.close()


class SandboxTest(unittest.TestCase):
    """sandbox/approval_mode. The rungs themselves need real namespaces and are
    validated on a Linux host; these cover the pure logic and the inherit-first
    contract, which is where the subtle failures live."""

    def setUp(self):
        T.proc.clear(); T._children.clear()
        self._init_ns = T.INIT_NS
        # The real probe argv. NB anything with a trailing ' claude' matches the
        # claude_code pattern first (_AGENT_PATTERNS is first-match-wins and
        # bwrap is LAST), which is why this is the sandbox probe's own command.
        T.proc_add(100, 1, comm='bwrap', args='bwrap --ro-bind / / /bin/true',
                   user='u', uid=1000)
        T.proc[100].update(sandbox='sandboxed', sandbox_src='userns:exec',
                           sandbox_detail='user,mnt')
        T.proc_add(101, 100, comm='node', args='node /x/.claude/cli.js',
                   user='u', uid=1000)

    def tearDown(self):
        T.INIT_NS = self._init_ns

    # --- inherit-first, the same three cases the cwd precedent is tested for --
    def test_sandbox_inherits_from_parent(self):
        T._inherit_sandbox(T.proc[101])
        self.assertEqual(T.proc[101]['sandbox'], 'sandboxed')
        self.assertEqual(T.proc[101]['sandbox_detail'], 'user,mnt')
        self.assertEqual(T.proc[101]['sandbox_src'], 'inherit')

    def test_sandbox_inherit_does_not_overwrite_a_read(self):
        T.proc_add(102, 100, comm='sh', args='sh', user='u', uid=1000)
        T.proc[102].update(sandbox='unsandboxed', sandbox_src='none:exec')
        T._inherit_sandbox(T.proc[102])
        self.assertEqual(T.proc[102]['sandbox'], 'unsandboxed')
        self.assertEqual(T.proc[102]['sandbox_src'], 'none:exec')

    def test_sandbox_absent_when_parent_has_none(self):
        T.proc_add(200, 1, comm='bash', args='-bash', tty='pts/1', user='h', uid=1004)
        T.proc_add(201, 200, comm='ls', args='ls', user='h', uid=1004)
        T._inherit_sandbox(T.proc[201])
        self.assertIsNone(T.proc[201]['sandbox'])

    def test_unreadable_is_null_not_unsandboxed(self):
        """A ptrace-gated read must NOT read as 'not sandboxed' -- that would
        deflate the rate for every user but the collector's owner."""
        v, src, detail = T.classify_sandbox(2 ** 30)      # no such pid
        self.assertIsNone(v)
        self.assertEqual(src, 'denied')

    def test_no_baseline_means_unknown_not_unsandboxed(self):
        T.INIT_NS = {}
        v, src, _ = T.classify_sandbox(os.getpid())
        self.assertNotEqual(v, 'unsandboxed')

    # --- ancestry is separate from ground truth ------------------------------
    def test_ancestry_finds_bwrap_through_a_self_classifying_child(self):
        """The case the collector could not see before: resolve() returns early
        when claude_code self-classifies, so agent_type is 'claude_code' and the
        bwrap fact lived only in tree_root_pid."""
        self.assertTrue(T.resolve(101))
        self.assertEqual(T.proc[101]['agent_type'], 'claude_code')
        self.assertEqual(T.sandbox_ancestry_of(T.proc[101]), 'bwrap')

    def test_probe_signature_is_ancestry_bwrap_plus_unsandboxed(self):
        """findings/login.md's wedge case: `bwrap --ro-bind / /` is a capability
        PROBE, not a confinement. Ancestry and ground truth must disagree here,
        which is the whole reason both are emitted."""
        T.resolve(101)
        T.proc[101].update(sandbox='unsandboxed', sandbox_src='none:exec')
        ident = T._base_identity(T.proc[101], 'agent', 'ancestry')
        self.assertEqual(ident['sandbox_ancestry'], 'bwrap')
        self.assertEqual(ident['sandbox'], 'unsandboxed')

    def test_ancestry_none_for_a_plain_human(self):
        T.proc_add(300, 1, comm='bash', args='-bash', tty='pts/2', user='h', uid=1004)
        self.assertIsNone(T.sandbox_ancestry_of(T.proc[300]))

    # --- approval_mode, split from autonomous --------------------------------
    def test_approval_only_flags(self):
        for a in ('claude --dangerously-skip-permissions',
                  'codex --ask-for-approval never', 'x --yolo',
                  'claude --permission-mode bypassPermissions'):
            mode, sb = C.approval_mode_of(a)
            self.assertEqual(mode, 'bypassed', a)
            self.assertFalse(sb, a)

    def test_flags_that_also_disable_the_sandbox(self):
        for a in ('codex --dangerously-bypass-approvals-and-sandbox',
                  'codex --sandbox danger-full-access', 'codex --sandbox danger'):
            mode, sb = C.approval_mode_of(a)
            self.assertEqual(mode, 'bypassed', a)
            self.assertTrue(sb, a)

    def test_autonomous_is_unchanged_by_the_split(self):
        """Backward comparability: every pattern that made is_autonomous true
        before must still make it true, or existing findings shift under us."""
        for a in ('c --dangerously-skip-permissions',
                  'c --dangerously-bypass-approvals-and-sandbox',
                  'c danger-full-access', 'c --ask-for-approval never',
                  'c --yolo', 'c --sandbox danger'):
            self.assertTrue(C.is_autonomous(a), a)
        self.assertFalse(C.is_autonomous('claude --help'))
        self.assertFalse(C.is_autonomous(None))

    # --- the ordering trap: the predicate must fire on a launcher's CHILD -----
    def test_read_fires_on_a_child_of_a_launcher(self):
        """bwrap execs, THEN unshares, THEN execs the payload. If the predicate
        only fired on the launcher's own comm, the read would return HOST
        namespaces and inheritance would mark the whole sandboxed tree
        unsandboxed -- the exact opposite of the truth."""
        T.proc_add(400, 1, comm='bwrap', args='bwrap --ro-bind / / /bin/sh')
        T.proc[400]['sandbox'] = 'unsandboxed'          # read before it unshared
        T.proc_add(401, 400, comm='sh', args='sh -c x')
        T.proc[401]['sandbox'] = 'unsandboxed'          # inherited, and wrong
        self.assertTrue(T._need_sandbox_read(T.proc[401]))

    def test_read_fires_on_the_launcher_itself(self):
        T.proc_add(402, 1, comm='unshare', args='unshare --mount sleep 1')
        T.proc[402]['sandbox'] = 'unsandboxed'
        self.assertTrue(T._need_sandbox_read(T.proc[402]))

    def test_read_is_skipped_for_an_ordinary_inherited_process(self):
        """The cost control: an ordinary exec under a known-state parent must NOT
        pay a /proc read, or this lands on the per-event path."""
        T.proc_add(403, 1, comm='bash', args='-bash')
        T.proc[403]['sandbox'] = 'unsandboxed'
        T.proc_add(404, 403, comm='ls', args='ls')
        T.proc[404]['sandbox'] = 'unsandboxed'
        self.assertFalse(T._need_sandbox_read(T.proc[404]))

    def test_unknown_state_alone_does_not_buy_a_read(self):
        """Cost control, measured: reading for EVERY unknown process charged
        ~15k extra /proc operations to a 3000-exec benchmark -- a reproducible
        ~12% fork/exec regression -- for processes the actor filter then dropped.
        An untracked process stays null, which is the honest value for something
        that is never emitted."""
        T.proc_add(405, 1, comm='ls', args='ls')
        self.assertFalse(T._need_sandbox_read(T.proc[405]))

    def test_unknown_state_does_fire_for_a_process_we_will_emit(self):
        T.proc_add(406, 1, comm='bash', args='-bash', tty='pts/9')
        self.assertTrue(T._need_sandbox_read(T.proc[406]))
        T.proc_add(407, 100, comm='git', args='git log')   # under the agent root
        T.proc[407]['agent_pid'] = 100
        self.assertTrue(T._need_sandbox_read(T.proc[407]))

    def test_approval_mode_is_derived_at_emit_not_only_at_exec(self):
        """A SEEDED process -- alive before the collector attached, so no exec
        event was ever seen -- must still report approval_mode. That is not a
        corner case: it is the long-running agent session a collector restart
        re-seeds, i.e. exactly what session_uuid exists to keep stable. Found in
        live validation, where such a root reported autonomous=True and
        approval_mode=null on the same record."""
        T.proc_add(500, 1, comm='node',
                   args='node /x/.claude/cli.js --dangerously-skip-permissions')
        T.resolve(500)
        i = T._base_identity(T.proc[500], 'agent', 'ancestry')
        self.assertEqual(i['approval_mode'], 'bypassed')
        self.assertEqual(i['approval_src'], 'argv')
        self.assertTrue(i['autonomous'])      # the two must never disagree

    def test_approval_mode_absent_for_a_plain_agent(self):
        T.proc_add(501, 1, comm='node', args='node /x/.claude/cli.js')
        T.resolve(501)
        i = T._base_identity(T.proc[501], 'agent', 'ancestry')
        self.assertIsNone(i['approval_mode'])
        self.assertIsNone(i['approval_src'])
        self.assertFalse(i['autonomous'])

    def test_identity_carries_the_new_fields(self):
        ident = T._base_identity(T.proc[100], 'agent', 'ancestry')
        for k in ('sandbox', 'sandbox_src', 'sandbox_detail', 'sandbox_ancestry',
                  'approval_mode', 'approval_src'):
            self.assertIn(k, ident)
        self.assertNotIn('env_flags', ident)

    def test_an_agent_roots_environment_is_never_read(self):
        """/proc/<pid>/environ holds another user's API keys next to the names;
        the collector does not open it at all, not even for the names."""
        import builtins
        T.proc_add(600, 1, comm='node', args='node /x/.claude/cli.js',
                   argv_source='kernel', argv_ktime=7, user='u', uid=1000)
        self.assertTrue(T.resolve(600))
        self.assertTrue(T.proc[600]['is_agent'])
        opened, real_open = [], builtins.open

        def spy(path, *a, **kw):
            opened.append(str(path))
            return real_open(path, *a, **kw)
        builtins.open = spy
        try:
            T.on_exec(Ev(pid=600, ppid=1, uid=1000, comm=b'node', ktime_ns=7,
                         has_tty=0, tty=b'', cgid=0))
        finally:
            builtins.open = real_open
        self.assertEqual([p for p in opened if p.endswith('/environ')], [])

    def test_approval_comes_from_argv_alone(self):
        T.proc_add(601, 1, comm='node', args='node /x/.claude/cli.js')
        T.resolve(601)
        self.assertEqual(T._approval_of(T.proc[601]), (None, None))


class CommandTest(unittest.TestCase):
    """`command` is the executable's name, whole. The kernel's comm stops at 15
    characters; one day on madsys-gpu1 had 115 records with a 15-character comm
    (code-2242ebbb54, codex-linux-san, git-remote-http, ...), and for most of them
    the full name was in argv all along."""

    def setUp(self):
        T.proc.clear(); T._children.clear()
        T._dropped['n'], T._dropped['commands'] = 0, {}
        self._saved = (T.writer, T.read_stat)

    def tearDown(self):
        T.writer, T.read_stat = self._saved
        T.proc.clear(); T._children.clear()
        T._dropped['n'], T._dropped['commands'] = 0, {}

    def _command(self, comm, args):
        T.proc_del(800)                  # proc_add would return a previous entry as is
        T.proc_add(800, 1, comm=comm, args=args)
        return T._base_identity(T.proc[800], 'human', 'tty')['command']

    def test_the_identity_says_command_not_comm(self):
        T.proc_add(800, 1, comm='git', args='git status')
        ident = T._base_identity(T.proc[800], 'human', 'tty')
        self.assertEqual(ident['command'], 'git')
        self.assertNotIn('comm', ident)

    def test_a_cut_comm_is_completed_from_argv0(self):
        self.assertEqual(self._command('codex-linux-san', '/opt/codex/codex-linux-sandbox --x'),
                         'codex-linux-sandbox')

    def test_a_script_is_completed_from_the_path_after_its_interpreter(self):
        """binfmt_script puts the interpreter in argv[0]; the comm is the script's."""
        self.assertEqual(self._command('91-release-upgr',
                                       '/bin/sh /etc/update-motd.d/91-release-upgrade'),
                         '91-release-upgrade')

    def test_a_name_that_exists_nowhere_longer_stays_as_the_kernel_has_it(self):
        """A thread name set with prctl was cut before it ever reached argv."""
        self.assertEqual(self._command('tokio-rt-worker', '/usr/bin/codex exec fix it'),
                         'tokio-rt-worker')

    def test_a_15_character_name_that_was_not_cut_stays(self):
        self.assertEqual(self._command('copilot-runtime', '/x/copilot-runtime --stdio'),
                         'copilot-runtime')

    def test_a_short_comm_is_never_rewritten_from_argv(self):
        """Only a comm at the kernel's limit can be a prefix. An interpreter's
        script argument can start with its name without being its name."""
        self.assertEqual(self._command('python3', 'python3 /x/python3-helper.py'), 'python3')
        self.assertEqual(self._command('bash', '-bash'), 'bash')

    def test_residency_names_processes_by_command(self):
        T.writer = W = FakeWriter()
        T.read_stat = _fake_stat
        full = 'code-2242ebbb54f8d95ca6af21c9e0b7d1ac'
        T.proc_add(100, 1, comm='node', args='node /x/.claude/cli.js', user='u', uid=1000)
        T.proc_add(101, 100, comm='code-2242ebbb54', args='/home/u/.vscode-server/%s -x' % full,
                   user='u', uid=1000)
        T.residency_tick(None)
        (res,) = W.of('residency')
        self.assertEqual(res['by_command'], {'node': 1, full: 1})
        self.assertNotIn('by_comm', res)
        self.assertEqual(sorted(p['command'] for p in res['top_procs']), sorted([full, 'node']))

    def test_the_dropped_bucket_counts_by_command(self):
        T.proc_add(820, 1, comm='git-remote-http',
                   args='/usr/lib/git-core/git-remote-https origin https://x')
        T._note_dropped(T.proc[820])
        self.assertEqual(T._dropped['commands'], {'git-remote-https': 1})

    def test_a_launcher_past_15_characters_triggers_the_sandbox_read(self):
        """SANDBOX_COMMS lists codex-linux-sandbox, which the kernel only ever
        reports as codex-linux-san: matched on comm, this rung could never fire."""
        T.proc_add(810, 1, comm='codex-linux-san', args='/opt/codex/codex-linux-sandbox --x')
        T.proc_add(811, 810, comm='sh', args='sh -c x')
        T.proc[811]['sandbox'] = 'unsandboxed'
        self.assertTrue(T._need_sandbox_read(T.proc[811]))


class _ArgvT(ctypes.Structure):
    """The kernel's argv_t as BCC decodes it off the argv ring."""
    _fields_ = [('pid', ctypes.c_uint32), ('len', ctypes.c_uint32),
                ('raw_len', ctypes.c_uint32), ('_pad', ctypes.c_uint32),
                ('ktime_ns', ctypes.c_uint64), ('buf', ctypes.c_char * 4096)]


def _argv_ev(pid, data, raw_len=None):
    # memmove into a ctypes buffer does no bounds check: an oversize `data` would
    # write past it and corrupt the heap (glibc aborts; macOS says nothing)
    assert len(data) <= _ArgvT.buf.size, len(data)
    a = _ArgvT()
    a.pid, a.len, a.ktime_ns = pid, len(data), 1
    a.raw_len = len(data) if raw_len is None else raw_len
    ctypes.memmove(ctypes.addressof(a) + _ArgvT.buf.offset, data, len(data))
    return a


class ArgsTruncationTest(unittest.TestCase):
    def setUp(self):
        T.proc.clear(); T._children.clear()
        self._saved = (T.ARGS_MAXLEN, T._tgid_of)
        T._tgid_of = lambda pid: pid

    def tearDown(self):
        T.ARGS_MAXLEN, T._tgid_of = self._saved

    def test_truncated_decode_drops_the_partial_token(self):
        """The bug this exists to stop: a clamped buffer ends mid-token with no
        trailing NUL, so the half-token reads as a whole argument -- and
        classify() runs on it. '/opt/tools/claude-wrapper' cut to
        '/opt/tools/claude' matches the claude_code pattern's end anchor."""
        buf = b'/bin/sh\x00-c\x00/opt/tools/claude-wrapper'   # cut mid-token
        self.assertEqual(T.decode_argv(buf), '/bin/sh -c /opt/tools/claude-wrapper')
        self.assertEqual(T.decode_argv(buf, truncated=True), '/bin/sh -c')

    def test_truncation_no_longer_fabricates_an_agent(self):
        cut = b'/bin/sh\x00-c\x00/opt/tools/claude'
        self.assertIsNotNone(C.classify(T.decode_argv(cut)))            # would have
        self.assertIsNone(C.classify(T.decode_argv(cut, truncated=True)))

    def test_untruncated_decode_is_unchanged(self):
        buf = b'git\x00status\x00'
        self.assertEqual(T.decode_argv(buf), 'git status')
        self.assertEqual(T.decode_argv(buf, truncated=False), 'git status')

    def test_single_token_truncated_yields_empty_not_garbage(self):
        self.assertEqual(T.decode_argv(b'/usr/bin/somethinglon', truncated=True), '')

    # --- the emitted pair ----------------------------------------------------
    def _ident(self, **kw):
        T.proc_add(700, 1, comm='sh', **kw)
        return T._base_identity(T.proc[700], 'human', 'tty')

    def test_len_and_flag_for_a_short_command(self):
        T.proc_add(700, 1, comm='sh', args='ls -l')
        T.proc[700]['args_raw_len'] = 5
        i = T._base_identity(T.proc[700], 'human', 'tty')
        self.assertEqual(i['args_len'], 5)
        self.assertFalse(i['args_truncated'])

    def test_no_emit_cap_by_default(self):
        """A /proc read is uncapped, so a 9000-byte command line arrives whole and
        goes out whole."""
        T.proc_add(701, 1, comm='sh', args='x' * 9000, argv_source='proc')
        T.proc[701]['args_raw_len'] = 9000
        i = T._base_identity(T.proc[701], 'human', 'tty')
        self.assertEqual((len(i['args']), i['args_len']), (9000, 9000))
        self.assertFalse(i['args_truncated'])

    def test_an_emit_cap_still_applies_when_set(self):
        T.ARGS_MAXLEN = 2048
        T.proc_add(701, 1, comm='sh', args='x' * 9000, argv_source='proc')
        T.proc[701]['args_raw_len'] = 9000
        i = T._base_identity(T.proc[701], 'human', 'tty')
        self.assertEqual(len(i['args']), 2048)
        self.assertTrue(i['args_truncated'])

    def test_a_kernel_clamped_argv_is_truncated_with_no_emit_cap(self):
        """The in-kernel read stops at ARGV_KMAX. Without an emit cap to compare
        against, args_truncated must still say the kernel cut it, or a clipped
        command line reads as complete."""
        T.proc_add(702, 1, comm='sh')
        clipped = b'/bin/sh\x00-c\x00' + b'x' * 4085          # 4096 bytes, cut mid-token
        T.on_argv(_argv_ev(702, clipped, raw_len=9000))
        i = T._base_identity(T.proc[702], 'human', 'tty')
        self.assertEqual((i['args'], i['args_len']), ('/bin/sh -c', 9000))
        self.assertTrue(i['args_truncated'])

    def test_a_kernel_argv_that_fit_is_complete(self):
        T.proc_add(703, 1, comm='git')
        T.on_argv(_argv_ev(703, b'git\x00status\x00'))
        i = T._base_identity(T.proc[703], 'human', 'tty')
        self.assertEqual(i['args'], 'git status')
        self.assertFalse(i['args_truncated'])

    def test_a_fork_inherits_the_clipped_argv_and_says_so(self):
        T.proc_add(704, 1, comm='sh')
        T.on_argv(_argv_ev(704, b'/bin/sh\x00-c\x00' + b'x' * 4085, raw_len=9000))
        T.on_fork(Ev(pid=705, ppid=704, uid=1000))
        self.assertTrue(T._base_identity(T.proc[705], 'human', 'tty')['args_truncated'])

    def test_a_new_exec_forgets_the_old_images_argv_lengths(self):
        """exec replaces the image. Until the new argv arrives its length is
        unknown -- null -- not the previous image's 9000 and its clamp."""
        ingest = T._ingest_proc
        T._ingest_proc = lambda pid, ppid=None: False        # no /proc on the test host
        try:
            T.proc_add(706, 1, comm='sh')
            T.on_argv(_argv_ev(706, b'/bin/sh\x00-c\x00' + b'x' * 4085, raw_len=9000))
            T.on_exec(Ev(pid=706, ppid=1, uid=1000, comm=b'git', ktime_ns=2,
                         has_tty=0, tty=b'', cgid=0))
            i = T._base_identity(T.proc[706], 'human', 'tty')
            self.assertEqual((i['args'], i['args_len'], i['args_truncated']), (None, None, None))
            T.on_argv(_argv_ev(706, b'git\x00log\x00'))
            i = T._base_identity(T.proc[706], 'human', 'tty')
            self.assertEqual((i['args'], i['args_len'], i['args_truncated']),
                             ('git log', 8, False))
        finally:
            T._ingest_proc = ingest

    def test_unknown_raw_len_is_null_not_false(self):
        """null = 'not measured'. false would claim 'measured and complete',
        which a reader parsing a pre-raw_len file must never be told."""
        T.proc_add(702, 1, comm='sh', args='ls')
        i = T._base_identity(T.proc[702], 'human', 'tty')
        self.assertIsNone(i['args_len'])
        self.assertIsNone(i['args_truncated'])


class SlurmArgsTest(unittest.TestCase):
    """P1-5. ARGV ONLY -- the #SBATCH scan the request asked for is deliberately
    not built: the script path comes from work_dir, a path read from /proc, and
    resolving one of those is what hangs this collector on a wedged autofs mount.
    The directives are recovered by joining submit.job_id to the slurm_jobs
    census, which is authoritative anyway."""

    def test_the_six_slurm_walltime_spellings(self):
        for tok, want in (('30', 1800), ('5:30', 330), ('1:2:3', 3723),
                          ('2-3', 183600), ('2-3:4', 183840), ('2-3:4:5', 183845)):
            self.assertEqual(SA.parse_cli_time(tok), want, tok)

    def test_bad_walltime_is_none_not_a_guess(self):
        self.assertIsNone(SA.parse_cli_time('not-a-time'))

    def test_full_request(self):
        r = SA.parse_submit_args(
            'sbatch -p gpu --gres=gpu:2 -t 1-00:00:00 --array=1-4 --mem=8G -c 4 run.sh')
        self.assertEqual(r['partition'], 'gpu')
        self.assertEqual(r['gpus'], 2)
        self.assertEqual(r['array'], '1-4')
        self.assertEqual(r['time_limit_s'], 86400)
        self.assertEqual(r['mem'], '8G')
        self.assertEqual(r['cpus_per_task'], 4)
        self.assertEqual(r['req_src'], 'argv')

    def test_gres_count_forms(self):
        """`--gres=gpu:2` must be 2, not 1: an optional TYPE group will swallow
        the ':2' unless the type is required to be letter-led."""
        for a, want in (('--gres=gpu:2', 2), ('--gres=gpu:a100:4', 4),
                        ('--gres=gpu', 1), ('--gres gpu:8', 8),
                        ('--gpus-per-node=2', 2), ('--gpus 3', 3)):
            self.assertEqual(SA.parse_submit_args('sbatch %s x.sh' % a)['gpus'], want, a)

    def test_nothing_requested_means_req_src_none(self):
        """Absent != zero. None here means 'not on the command line', which is
        not the same as 'not requested' -- it may be in the script."""
        r = SA.parse_submit_args('sbatch plain.sh')
        self.assertIsNone(r['req_src'])
        self.assertIsNone(r['partition'])
        self.assertIsNone(r['gpus'])

    def test_no_args(self):
        self.assertIsNone(SA.parse_submit_args(None)['req_src'])


class SessionUuidTest(unittest.TestCase):
    """session_uuid must survive a collector restart and must NOT merge two
    processes that happened to reuse a pid."""

    def setUp(self):
        T.proc.clear(); T._children.clear()
        self._boot = T.BOOT_ID
        T.BOOT_ID = 'boot-aaaa'
        T.proc_add(100, 1, comm='node', args='node /x/.claude/cli.js')
        T.proc[100]['start_ticks'] = 555

    def tearDown(self):
        T.BOOT_ID = self._boot

    def test_stable_across_calls(self):
        """A restarted collector re-seeds live pids and must recompute the same
        digest — that is the whole point of the field."""
        self.assertEqual(T.session_uuid(100), T.session_uuid(100))

    def test_pid_recycling_does_not_merge_sessions(self):
        """Same pid, different start time = a different process. session_key
        ('apid:100') cannot tell these apart; session_uuid must."""
        first = T.session_uuid(100)
        T.proc[100]['start_ticks'] = 556
        self.assertNotEqual(first, T.session_uuid(100))

    def test_differs_across_boots(self):
        first = T.session_uuid(100)
        T.BOOT_ID = 'boot-bbbb'
        self.assertNotEqual(first, T.session_uuid(100))

    def test_null_rather_than_a_partial_digest(self):
        """No boot_id or no start time => None, not a digest over a missing
        component that would silently differ from the previous run's."""
        T.BOOT_ID = None
        self.assertIsNone(T.session_uuid(100))
        T.BOOT_ID = 'boot-aaaa'
        self.assertIsNone(T.session_uuid(None))

    def test_identity_carries_it_next_to_session_key(self):
        T.proc[100].update(agent_pid=100, agent_type='claude_code', is_agent=True, depth=0,
                           attributed=True)
        ident = T._base_identity(T.proc[100], 'agent', 'ancestry')
        self.assertEqual(ident['session_key'], 'apid:100')
        self.assertEqual(ident['session_uuid'], T.session_uuid(100))


class SockDiagTest(unittest.TestCase):
    def test_tcp_info_fields(self):
        i = sockdiag.parse_tcp_info(_mk_tcp_info())
        self.assertEqual(i['rtt_us'], 31200)
        self.assertEqual(i['bytes_received'], 41882133)
        self.assertEqual(i['bytes_acked'], 219004)
        self.assertEqual(i['segs_in'], 1200)
        self.assertEqual(i['last_data_sent'], 4100)

    def test_tcp_info_short_blob_is_partial_not_fatal(self):
        i = sockdiag.parse_tcp_info(_mk_tcp_info()[:80])
        self.assertIn('rtt_us', i)
        self.assertNotIn('bytes_received', i)

    def test_one_message(self):
        rows = sockdiag.parse_messages(_mk_msg(info=_mk_tcp_info()))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r['saddr'], r['sport']), ('10.243.49.205', 45964))
        self.assertEqual((r['daddr'], r['dport']), ('140.82.114.6', 443))
        self.assertEqual(r['state'], 'ESTABLISHED')
        self.assertEqual(r['uid'], 1004)
        self.assertEqual(r['info']['rtt_us'], 31200)

    def test_two_messages_and_alignment(self):
        blob = _mk_msg(info=_mk_tcp_info()) + _mk_msg(sport=1, dport=2, info=None)
        rows = sockdiag.parse_messages(blob)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[1]['info'])

    def test_done_terminates(self):
        done = struct.pack('=IHHII', 16, sockdiag.NLMSG_DONE, 2, 1, 0)
        rows = sockdiag.parse_messages(_mk_msg(info=None) + done + _mk_msg(sport=9))
        self.assertEqual(len(rows), 1)
        self.assertTrue(sockdiag._has_done(done))

    def test_ipv6(self):
        rows = sockdiag.parse_messages(_mk_msg(saddr='2001:db8::1', daddr='2606:50c0::1',
                                               family=socket.AF_INET6, info=None))
        self.assertEqual(rows[0]['family'], 'inet6')
        self.assertEqual(rows[0]['saddr'], '2001:db8::1')

    def test_truncated_buffer_does_not_raise(self):
        self.assertEqual(sockdiag.parse_messages(b'\x00' * 8), [])
        self.assertEqual(sockdiag.parse_messages(_mk_msg(info=None)[:20]), [])

    def test_key_of_matches_the_bpf_join(self):
        r = sockdiag.parse_messages(_mk_msg(info=None))[0]
        self.assertEqual(sockdiag.key_of(r), ('10.243.49.205', 45964, '140.82.114.6', 443))

    def test_conn_stats_shape(self):
        r = sockdiag.parse_messages(_mk_msg(info=_mk_tcp_info()))[0]
        s = T._conn_stats(r)
        self.assertEqual(s['rtt_ms'], 31.2)
        self.assertEqual(s['rx_bytes'], 41882133)
        self.assertEqual(s['idle_send_s'], 4.1)
        empty = T._conn_stats(None)
        self.assertEqual(set(empty.values()), {None})
        self.assertEqual(set(s.keys()), set(empty.keys()))   # same shape either way


# ---------------------------------------------------------------------------
# schema 7: network bytes by endpoint -- the kernel half, compiled and run
# ---------------------------------------------------------------------------
#
# The endpoint logic lives in BPF C that only a root host can load. Asserting on
# its text would prove only that the source is the source, so instead the
# network section of the generated program is compiled with the host compiler
# against tests/bpf_mock.h (map semantics, bpf_probe_read as a plain copy) and
# its probes are driven from a script: fake sockets laid out at the BTF offsets,
# fake msghdrs, then entry/return calls in the order the kernel makes them.

_CC = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
_MOCK_H = str(_ROOT / 'tests' / 'bpf_mock.h')
_NET_BIN_CACHE = {}
_NET_BIN_DIR = []

_NET_DRIVER = r'''
#define OFF_FAMILY %(f_family)d
#define OFF_NUM %(f_num)d
#define OFF_DPORT %(f_dport)d
#define OFF_DADDR %(f_daddr)d
#define OFF_V6_DADDR %(f_v6)d

struct fake { char name[32]; unsigned char sk[512]; unsigned char sa[64]; void *msg[8]; };
static struct fake fakes[64];
static int nfakes;

static struct fake *fake_of(const char *name)
{
    for (int i = 0; i < nfakes; i++)
        if (!strcmp(fakes[i].name, name))
            return &fakes[i];
    struct fake *f = &fakes[nfakes++];
    memset(f, 0, sizeof(*f));
    snprintf(f->name, sizeof(f->name), "%%s", name);
    return f;
}

static void unhex(const char *s, unsigned char *out, int n)
{
    for (int i = 0; i < n && s[2 * i] && s[2 * i + 1]; i++) {
        unsigned v = 0;
        sscanf(s + 2 * i, "%%2x", &v);
        out[i] = (unsigned char)v;
    }
}

static u16 be16(u16 v) { return (u16)((v >> 8) | (v << 8)); }

static unsigned long arg_of(const char *tok)
{
    if (!strncmp(tok, "sock:", 5))
        return (unsigned long)fake_of(tok + 5)->sk;
    if (!strncmp(tok, "msg:", 4))
        return (unsigned long)fake_of(tok + 4)->msg;
    return (unsigned long)strtoull(tok, 0, 0);
}

%(probe_table)s

int main(void)
{
    char line[512];
    while (fgets(line, sizeof(line), stdin)) {
        char cmd[16] = "", a[64] = "", b[64] = "", c[64] = "", d[64] = "", e[80] = "";
        if (sscanf(line, "%%15s %%63s %%63s %%63s %%63s %%79s", cmd, a, b, c, d, e) < 1)
            continue;
        if (!strcmp(cmd, "sock")) {            /* sock NAME FAMILY LPORT RPORT ADDRHEX */
            struct fake *f = fake_of(a);
            u16 fam = (u16)atoi(b), lport = (u16)atoi(c), rport = be16((u16)atoi(d));
            memcpy(f->sk + OFF_FAMILY, &fam, 2);
            memcpy(f->sk + OFF_NUM, &lport, 2);        /* host order, as skc_num is */
            memcpy(f->sk + OFF_DPORT, &rport, 2);      /* network order */
            unhex(e, f->sk + (fam == 2 ? OFF_DADDR : OFF_V6_DADDR), fam == 2 ? 4 : 16);
        } else if (!strcmp(cmd, "msg")) {       /* msg NAME FAMILY PORT ADDRHEX; family 0 = no msg_name */
            struct fake *f = fake_of(a);
            u16 fam = (u16)atoi(b), port = be16((u16)atoi(c));
            f->msg[0] = 0;
            if (fam) {
                memset(f->sa, 0, sizeof(f->sa));
                memcpy(f->sa, &fam, 2);                /* sa_family is host order */
                memcpy(f->sa + 2, &port, 2);
                unhex(d, f->sa + (fam == 2 ? 4 : 8), fam == 2 ? 4 : 16);
                f->msg[0] = f->sa;
            }
        } else if (!strcmp(cmd, "birth")) {     /* birth SOCKNAME INBOUND */
            %(birth_cmd)s
        } else if (!strcmp(cmd, "call")) {      /* call FN PID_TGID ARG1 ARG2 RC */
            struct pt_regs r = { arg_of(c), arg_of(d), (unsigned long)strtoull(e, 0, 0) };
            int hit = 0;
            mock_pid_tgid = strtoull(b, 0, 0);
            for (int i = 0; probes[i].name; i++)
                if (!strcmp(probes[i].name, a)) {
                    probes[i].fn(&r);
                    hit = 1;
                }
            if (!hit) {
                printf("{\"error\": \"no probe %%s\"}\n", a);
                return 2;
            }
        }
    }
%(dump)s
    return 0;
}
'''

_DUMP_NETB = r'''
    for (unsigned i = 0; i < sizeof(netb_slots) / sizeof(netb_slots[0]); i++)
        if (netb_slots[i].used)
            printf("{\"map\": \"netb\", \"tgid\": %u, \"tx\": %llu, \"rx\": %llu, \"calls\": %llu}\n",
                   netb_slots[i].k, (unsigned long long)netb_slots[i].v.tx,
                   (unsigned long long)netb_slots[i].v.rx, (unsigned long long)netb_slots[i].v.calls);
'''
_DUMP_NETP = r'''
    for (unsigned i = 0; i < sizeof(netp_slots) / sizeof(netp_slots[0]); i++)
        if (netp_slots[i].used) {
            struct netp_key_t *k = &netp_slots[i].k;
            printf("{\"map\": \"netp\", \"tgid\": %u, \"proto\": %u, \"flags\": %u, \"port\": %u, \"addr\": \"",
                   k->tgid, k->proto, k->flags, k->port);
            for (int j = 0; j < 16; j++)
                printf("%02x", k->addr[j]);
            printf("\", \"tx\": %llu, \"rx\": %llu, \"calls\": %llu}\n",
                   (unsigned long long)netp_slots[i].v.tx, (unsigned long long)netp_slots[i].v.rx,
                   (unsigned long long)netp_slots[i].v.calls);
        }
    {
        u32 i0 = 0, i1 = 1, i2 = 2;
        printf("{\"map\": \"drop\", \"calls\": %llu, \"tx\": %llu, \"rx\": %llu}\n",
               (unsigned long long)*netp_drop.lookup(&i0), (unsigned long long)*netp_drop.lookup(&i1),
               (unsigned long long)*netp_drop.lookup(&i2));
    }
    printf("{\"map\": \"netp_args\", \"n\": %d}\n", netp_args_count());
'''
_DUMP_NEST = r'''
    printf("{\"map\": \"netb_nest\", \"n\": %d}\n", netb_nest_count());
'''


def _net_binary(feats, offs):
    """Compile (once per configuration) the network section for `feats`."""
    key = (tuple(sorted(feats)), T.NETPEER_MAX, tuple(T.EPH_RANGE),
           json.dumps(offs, sort_keys=True))
    exe = _NET_BIN_CACHE.get(key)
    if exe:
        return exe
    if not _NET_BIN_DIR:
        d = tempfile.mkdtemp(prefix='ebpfm_netc_')
        import atexit
        atexit.register(shutil.rmtree, d, True)
        _NET_BIN_DIR.append(d)
    net = T.build_net_c(set(feats), offs)
    tcp_on = bool((T.TCP_BLOCKS | {'tcp_accept'}) & set(feats))
    probes = re.findall(r'int ((?:kprobe|kretprobe)__\w+)\(struct pt_regs \*ctx\)', net)
    table = ('static struct { const char *name; int (*fn)(struct pt_regs *); } probes[] = {\n'
             + ''.join('    {"%s", %s},\n' % (p, p) for p in probes) + '    {0, 0}\n};')
    birth = ('struct tcp_meta_t m = {}; u64 skp = (u64)fake_of(a)->sk;\n'
             '            m.inbound = (u8)atoi(b);\n'
             '            tcp_birth.update(&skp, &m);') if tcp_on else 'return 3;'
    dump = _DUMP_NETB
    if T.NETPEER_BLOCKS & set(feats):
        dump += _DUMP_NETP
    if 'netbytes_udp6' in feats:
        dump += _DUMP_NEST
    sc = offs['sock_common']
    driver = _NET_DRIVER % {'f_family': sc['skc_family'], 'f_num': sc['skc_num'],
                            'f_dport': sc['skc_dport'], 'f_daddr': sc['skc_daddr'],
                            'f_v6': sc['skc_v6_daddr'], 'probe_table': table,
                            'birth_cmd': birth, 'dump': dump}
    path = os.path.join(_NET_BIN_DIR[0], 'net%d' % len(_NET_BIN_CACHE))
    with open(path + '.c', 'w') as f:
        f.write('#include "%s"\n%s\n%s\n%s' % (
            _MOCK_H, T.build_tcp_common_c() if tcp_on else '', net, driver))
    p = subprocess.run([_CC, '-std=gnu11', '-w', '-o', path, path + '.c'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if p.returncode:
        raise AssertionError('generated C did not compile:\n' + p.stderr[-3000:])
    _NET_BIN_CACHE[key] = path
    return path


def _run_net_c(feats, lines, offs=FAKE_OFFS):
    """Run a probe script against the compiled section; returns the maps."""
    p = subprocess.run([_net_binary(feats, offs)], input='\n'.join(lines) + '\n',
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    out = {'netp': [], 'netb': {}, 'drop': None, 'netp_args': None, 'netb_nest': None}
    for line in p.stdout.splitlines():
        r = json.loads(line)
        if 'error' in r:
            raise AssertionError(r['error'])
        kind = r.pop('map')
        if kind == 'netp':
            out['netp'].append(r)
        elif kind == 'netb':
            out['netb'][r['tgid']] = (r['tx'], r['rx'], r['calls'])
        elif kind == 'drop':
            out['drop'] = (r['calls'], r['tx'], r['rx'])
        else:
            out[kind] = r['n']
    if p.returncode:
        raise AssertionError('probe driver exited %d: %s' % (p.returncode, p.stderr))
    return out


def _pid_tgid(tgid, tid=None):
    return (tgid << 32) | (tgid if tid is None else tid)


def _hex(ip):
    return socket.inet_pton(socket.AF_INET6 if ':' in ip else socket.AF_INET, ip).hex()


def _key_addr(ip):
    """The 16-byte key field as the kernel fills it: a v4 address in the first four."""
    raw = socket.inet_pton(socket.AF_INET6 if ':' in ip else socket.AF_INET, ip)
    return (raw + b'\x00' * 16)[:16].hex()


def _sock(name, ip, lport, rport):
    return 'sock %s %d %d %d %s' % (name, 10 if ':' in ip else 2, lport, rport, _hex(ip))


def _msg(name, ip=None, port=0):
    if ip is None:
        return 'msg %s 0 0 -' % name
    return 'msg %s %d %d %s' % (name, 10 if ':' in ip else 2, port, _hex(ip))


def _call(fn, pid_tgid, a1='0', a2='0', rc=0):
    return 'call %s %d %s %s %d' % (fn, pid_tgid, a1, a2, rc)


def _pair(fn, pid_tgid, sock, msg='0', rc=0):
    """One send/recv as the kernel runs it: entry probe, then return probe."""
    return [_call('kprobe__' + fn, pid_tgid, 'sock:' + sock, msg),
            _call('kretprobe__' + fn, pid_tgid, rc=rc)]


@unittest.skipUnless(_CC, 'needs a C compiler to run the generated probes')
class NetEndpointKernelTest(unittest.TestCase):
    BASE = {'netbytes', 'netpeer_btf'}
    U6 = {'netbytes', 'netbytes_udp6', 'netpeer_btf'}
    APP = _pid_tgid(4242)

    def setUp(self):
        self._saved = (T.EPH_RANGE, T.NETPEER_MAX)
        T.EPH_RANGE = (32768, 60999)

    def tearDown(self):
        T.EPH_RANGE, T.NETPEER_MAX = self._saved

    # --- where the endpoint comes from ------------------------------------
    def test_a_tcp_send_lands_under_its_remote_endpoint(self):
        r = _run_net_c(self.BASE, [_sock('s', '104.18.0.1', 45000, 443)]
                       + _pair('tcp_sendmsg', self.APP, 's', rc=1500))
        self.assertEqual(r['netp'], [{'tgid': 4242, 'proto': 6, 'flags': 0, 'port': 443,
                                      'addr': _key_addr('104.18.0.1'),
                                      'tx': 1500, 'rx': 0, 'calls': 1}])
        self.assertEqual(r['netb'][4242], (1500, 0, 1))

    def test_a_receive_counts_as_rx(self):
        r = _run_net_c(self.BASE, [_sock('s', '104.18.0.1', 45000, 443)]
                       + _pair('tcp_recvmsg', self.APP, 's', rc=4000))
        self.assertEqual([(e['tx'], e['rx'], e['calls']) for e in r['netp']], [(0, 4000, 1)])

    def test_repeat_calls_to_one_endpoint_share_one_entry(self):
        lines = [_sock('s', '104.18.0.1', 45000, 443)]
        for rc in (100, 200, 300):
            lines += _pair('tcp_sendmsg', self.APP, 's', rc=rc)
        r = _run_net_c(self.BASE, lines)
        self.assertEqual([(e['tx'], e['calls']) for e in r['netp']], [(600, 3)])

    def test_ipv6_peer_comes_off_the_socket(self):
        r = _run_net_c(self.BASE, [_sock('s', '2606:4700::1111', 45000, 443)]
                       + _pair('tcp_sendmsg', self.APP, 's', rc=9))
        self.assertEqual([(e['flags'], e['port'], e['addr']) for e in r['netp']],
                         [(T.NETP_F_V6, 443, _key_addr('2606:4700::1111'))])

    def test_unconnected_udp_takes_the_peer_from_msg_name(self):
        """sendto() on a socket with no peer: its daddr is 0 and the destination
        exists only in msg->msg_name."""
        r = _run_net_c(self.BASE, [_sock('s', '0.0.0.0', 51000, 0), _msg('m', '8.8.8.8', 53)]
                       + _pair('udp_sendmsg', self.APP, 's', 'msg:m', rc=40))
        self.assertEqual([(e['proto'], e['port'], e['addr'], e['tx']) for e in r['netp']],
                         [(17, 53, _key_addr('8.8.8.8'), 40)])

    def test_recvfrom_names_the_sender(self):
        r = _run_net_c(self.BASE, [_sock('s', '0.0.0.0', 51000, 0), _msg('m', '8.8.4.4', 53)]
                       + _pair('udp_recvmsg', self.APP, 's', 'msg:m', rc=120))
        self.assertEqual([(e['port'], e['addr'], e['rx']) for e in r['netp']],
                         [(53, _key_addr('8.8.4.4'), 120)])

    def test_recvfrom_of_an_ipv6_sender(self):
        r = _run_net_c(self.BASE, [_sock('s', '::', 51000, 0), _msg('m', '2001:4860:4860::8888', 53)]
                       + _pair('udp_recvmsg', self.APP, 's', 'msg:m', rc=90))
        self.assertEqual([(e['flags'], e['port'], e['addr']) for e in r['netp']],
                         [(T.NETP_F_V6, 53, _key_addr('2001:4860:4860::8888'))])

    def test_a_connected_udp_socket_needs_no_msg_name(self):
        r = _run_net_c(self.BASE, [_sock('s', '1.1.1.1', 51000, 443), _msg('m')]
                       + _pair('udp_sendmsg', self.APP, 's', 'msg:m', rc=1200))
        self.assertEqual([(e['port'], e['addr']) for e in r['netp']],
                         [(443, _key_addr('1.1.1.1'))])

    def test_an_address_nobody_supplied_is_unattributed_not_dropped(self):
        """recv() on an unconnected UDP socket: no msg_name and no peer. The bytes
        still count, in their own bucket."""
        r = _run_net_c(self.BASE, [_sock('s', '0.0.0.0', 51000, 0), _msg('m')]
                       + _pair('udp_recvmsg', self.APP, 's', 'msg:m', rc=64))
        self.assertEqual([(e['flags'], e['port'], e['addr'], e['rx']) for e in r['netp']],
                         [(T.NETP_F_UNKNOWN, 0, '00' * 16, 64)])
        self.assertEqual(r['netb'][4242], (0, 64, 1))

    # --- direction ---------------------------------------------------------
    def test_inbound_traffic_is_keyed_by_the_local_port(self):
        """A client arrives from a random ephemeral port; keying on it would make
        one entry per connection. The local service port is the stable half."""
        lines = [_sock('a', '10.0.0.7', 8888, 50123), _sock('b', '10.0.0.7', 8888, 50999)]
        lines += _pair('tcp_recvmsg', self.APP, 'a', rc=10) + _pair('tcp_recvmsg', self.APP, 'b', rc=20)
        r = _run_net_c(self.BASE, lines)
        self.assertEqual([(e['flags'], e['port'], e['rx'], e['calls']) for e in r['netp']],
                         [(T.NETP_F_INBOUND, 8888, 30, 2)])

    def test_a_high_service_port_is_still_outbound(self):
        """Both ends in the ephemeral range (a gRPC server on 50051): nothing says
        inbound, so the remote port is kept."""
        r = _run_net_c(self.BASE, [_sock('s', '10.0.0.9', 40000, 50051)]
                       + _pair('tcp_sendmsg', self.APP, 's', rc=5))
        self.assertEqual([(e['flags'], e['port']) for e in r['netp']], [(0, 50051)])

    def test_the_ephemeral_range_is_the_hosts(self):
        T.EPH_RANGE = (40000, 45000)
        lines = [_sock('in', '10.0.0.7', 8888, 44000), _sock('out', '10.0.0.7', 8888, 35000)]
        lines += _pair('tcp_recvmsg', self.APP, 'in', rc=1) + _pair('tcp_recvmsg', self.APP, 'out', rc=1)
        r = _run_net_c(self.BASE, lines)
        self.assertEqual(sorted((e['flags'], e['port']) for e in r['netp']),
                         [(0, 35000), (T.NETP_F_INBOUND, 8888)])

    def test_an_accept_the_tcp_blocks_saw_overrides_the_port_guess(self):
        """The VS Code server case: it listens on an ephemeral-range port, so the
        guess calls its accepted connections outbound."""
        lines = [_sock('s', '127.0.0.1', 38465, 41000), 'birth s 1']
        lines += _pair('tcp_recvmsg', self.APP, 's', rc=7)
        r = _run_net_c(self.BASE | {'tcp_btf', 'tcp_accept'}, lines)
        self.assertEqual([(e['flags'], e['port']) for e in r['netp']],
                         [(T.NETP_F_INBOUND, 38465)])

    def test_a_connect_the_tcp_blocks_saw_is_outbound_whatever_the_ports(self):
        lines = [_sock('s', '10.0.0.7', 8888, 50123), 'birth s 0']   # the guess says inbound
        lines += _pair('tcp_sendmsg', self.APP, 's', rc=7)
        r = _run_net_c(self.BASE | {'tcp_btf'}, lines)
        self.assertEqual([(e['flags'], e['port']) for e in r['netp']], [(0, 50123)])

    # --- the entry/return hand-off ------------------------------------------
    def test_the_stash_is_per_thread(self):
        """Two threads of one process inside send at once: each return must pick
        up its OWN socket."""
        t1, t2 = _pid_tgid(4242, 4243), _pid_tgid(4242, 4244)
        lines = [_sock('a', '104.18.0.1', 45000, 443), _sock('b', '140.82.112.3', 45001, 443),
                 _call('kprobe__tcp_sendmsg', t1, 'sock:a'),
                 _call('kprobe__tcp_sendmsg', t2, 'sock:b'),
                 _call('kretprobe__tcp_sendmsg', t2, rc=2),
                 _call('kretprobe__tcp_sendmsg', t1, rc=1)]
        r = _run_net_c(self.BASE, lines)
        self.assertEqual(sorted((e['addr'], e['tx']) for e in r['netp']),
                         sorted([(_key_addr('104.18.0.1'), 1), (_key_addr('140.82.112.3'), 2)]))
        self.assertEqual(r['netp_args'], 0, 'every stash consumed')

    def test_a_failed_call_counts_nothing_and_clears_its_stash(self):
        lines = [_sock('s', '104.18.0.1', 45000, 443)] + _pair('tcp_recvmsg', self.APP, 's', rc=-11)
        # a return with no entry before it: the call was in flight at attach
        lines.append(_call('kretprobe__tcp_recvmsg', self.APP, rc=100))
        r = _run_net_c(self.BASE, lines)
        self.assertEqual(r['netp'], [])
        self.assertEqual(r['netb'][4242], (0, 100, 1), 'the totals never depend on the stash')
        self.assertEqual(r['netp_args'], 0)

    def test_the_int_return_is_sign_extended_from_32_bits(self):
        """Behavioural twin of the text check in BpfTextTest: whatever is in the
        upper half of rax must not be read as bytes."""
        r = _run_net_c(self.BASE, [_sock('s', '104.18.0.1', 45000, 443),
                                   _call('kprobe__tcp_recvmsg', self.APP, 'sock:s'),
                                   'call kretprobe__tcp_recvmsg %d 0 0 0xFFFFFFFB00001AF6' % self.APP])
        self.assertEqual(r['netp'][0]['rx'], 6902)
        self.assertEqual(r['netb'][4242], (0, 6902, 1))

    def test_a_full_table_counts_the_miss_and_the_totals_survive(self):
        T.NETPEER_MAX = 1
        lines = [_sock('a', '104.18.0.1', 45000, 443), _sock('b', '140.82.112.3', 45001, 443)]
        lines += _pair('tcp_sendmsg', self.APP, 'a', rc=10) + _pair('tcp_sendmsg', self.APP, 'b', rc=25)
        r = _run_net_c(self.BASE, lines)
        self.assertEqual([e['addr'] for e in r['netp']], [_key_addr('104.18.0.1')])
        self.assertEqual(r['drop'], (1, 25, 0))
        self.assertEqual(r['netb'][4242], (35, 0, 2))

    # --- IPv6 UDP ------------------------------------------------------------
    def test_native_ipv6_udp_is_counted(self):
        r = _run_net_c(self.U6, [_sock('s', '2606:4700::1111', 50000, 443)]
                       + _pair('udpv6_sendmsg', self.APP, 's', rc=900)
                       + _pair('udpv6_recvmsg', self.APP, 's', rc=1400))
        self.assertEqual(r['netb'][4242], (900, 1400, 2))
        self.assertEqual([(e['proto'], e['flags'], e['tx'], e['rx']) for e in r['netp']],
                         [(17, T.NETP_F_V6, 900, 1400)])

    def test_a_v4_mapped_send_is_counted_once(self):
        """udpv6_sendmsg hands a v4-mapped destination to udp_sendmsg on the SAME
        socket and thread. Both return probes fire; only one may count."""
        lines = [_sock('s6', '::ffff:1.2.3.4', 50000, 443), _msg('none'),
                 _msg('sin', '1.2.3.4', 443),
                 _call('kprobe__udpv6_sendmsg', self.APP, 'sock:s6', 'msg:none'),
                 _call('kprobe__udp_sendmsg', self.APP, 'sock:s6', 'msg:sin'),
                 _call('kretprobe__udp_sendmsg', self.APP, rc=1200),
                 _call('kretprobe__udpv6_sendmsg', self.APP, rc=1200)]
        r = _run_net_c(self.U6, lines)
        self.assertEqual(r['netb'][4242], (1200, 0, 1))
        self.assertEqual([(e['tx'], e['calls'], e['addr']) for e in r['netp']],
                         [(1200, 1, _key_addr('::ffff:1.2.3.4'))])
        self.assertEqual((r['netb_nest'], r['netp_args']), (0, 0))

    def test_without_the_endpoint_table_a_v4_mapped_send_still_counts_once(self):
        lines = [_sock('s6', '::ffff:1.2.3.4', 50000, 443),
                 _call('kprobe__udpv6_sendmsg', self.APP, 'sock:s6'),
                 _call('kprobe__udp_sendmsg', self.APP, 'sock:s6'),
                 _call('kretprobe__udp_sendmsg', self.APP, rc=1200),
                 _call('kretprobe__udpv6_sendmsg', self.APP, rc=1200)]
        r = _run_net_c({'netbytes', 'netbytes_udp6'}, lines)
        self.assertEqual(r['netb'][4242], (1200, 0, 1))

    def test_a_lost_return_does_not_silence_other_sockets(self):
        """kretprobes can be missed (maxactive). A nest entry left behind may only
        ever match its own v6 socket."""
        lines = [_sock('s6', '2606:4700::1111', 50000, 443), _sock('s4', '8.8.8.8', 51000, 53),
                 _call('kprobe__udpv6_sendmsg', self.APP, 'sock:s6')]     # its return never runs
        lines += _pair('udp_sendmsg', self.APP, 's4', rc=40)
        r = _run_net_c(self.U6, lines)
        self.assertEqual(r['netb'][4242], (40, 0, 1))
        self.assertEqual([(e['addr'], e['tx']) for e in r['netp']], [(_key_addr('8.8.8.8'), 40)])


class NetEndpointBlockTest(unittest.TestCase):
    """Which variant loads where. Design principle 4: a kernel that refuses one
    block loses that block alone."""

    def setUp(self):
        self._saved = (T.FEATURES_ENV, T.FEATURE_CACHE, T.BTF_OFFS)
        T.FEATURES_ENV, T.FEATURE_CACHE = 'auto', False
        T.BTF_OFFS = FAKE_OFFS

    def tearDown(self):
        T.FEATURES_ENV, T.FEATURE_CACHE, T.BTF_OFFS = self._saved

    def _probe(self, refuse=()):
        class FakeBPF(object):
            """Loads any program that does not contain a refused string."""
            def __init__(self, text=None, cflags=None):
                for r in refuse:
                    if r in text:
                        raise Exception('cannot attach %s' % r)

            def cleanup(self):
                pass
        _b, accepted, rejected = T.probe_features(FakeBPF, verbose=False)
        return accepted, rejected

    def test_everything_loads_where_nothing_is_refused(self):
        acc, rej = self._probe()
        self.assertTrue({'netbytes', 'netbytes_udp6', 'netpeer_btf'} <= acc)
        self.assertNotIn('netpeer_hdr', acc)
        self.assertTrue(rej['netpeer_hdr'].startswith('superseded'))

    def test_no_ipv6_udp_symbols_costs_only_that_block(self):
        acc, rej = self._probe(refuse=('udpv6_sendmsg',))
        self.assertTrue({'netbytes', 'netpeer_btf'} <= acc)
        self.assertIn('netbytes_udp6', rej)

    def test_without_btf_the_header_variant_loads(self):
        T.BTF_OFFS = {}
        acc, rej = self._probe()
        self.assertIn('netpeer_hdr', acc)
        self.assertIn('netpeer_btf', rej)

    def test_the_endpoint_table_needs_the_totals(self):
        acc, rej = self._probe(refuse=('kretprobe__tcp_sendmsg',))
        for blk in ('netbytes', 'netbytes_udp6', 'netpeer_btf', 'netpeer_hdr'):
            self.assertNotIn(blk, acc, blk)
        self.assertTrue(rej['netpeer_btf'].startswith('requires'))


class FeatureCacheTest(unittest.TestCase):
    """The feature cache is keyed by kernel release. A collector that gained
    blocks since the cache was written must probe them: loading the old set
    would never try the new ones, on every host that ran an older collector."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ebpfm_cache_')
        self._saved = (T.OUTPUT_DIR, T.FEATURES_ENV, T.FEATURE_CACHE, T.BTF_OFFS)
        T.OUTPUT_DIR, T.FEATURES_ENV, T.FEATURE_CACHE = self.dir, 'auto', True
        T.BTF_OFFS = FAKE_OFFS

    def tearDown(self):
        T.OUTPUT_DIR, T.FEATURES_ENV, T.FEATURE_CACHE, T.BTF_OFFS = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def _cache(self, **kw):
        with open(T._feature_cache_path(), 'w') as f:
            json.dump(kw, f)

    def _probe(self):
        class FakeBPF(object):
            def __init__(self, text=None, cflags=None):
                pass

            def cleanup(self):
                pass
        return T.probe_features(FakeBPF, verbose=False)[1]

    def test_a_cache_written_before_a_block_existed_is_reprobed(self):
        self._cache(kernel='k', features=['netbytes', 'tcp_btf'])
        self.assertIn('netpeer_btf', self._probe())

    def test_a_current_cache_is_used_as_is(self):
        self._cache(kernel='k', features=['netbytes'], blocks=list(T.BLOCK_NAMES))
        self.assertEqual(self._probe(), {'netbytes'})

    def test_a_fresh_probe_writes_a_cache_that_the_next_start_uses(self):
        first = self._probe()
        with open(T._feature_cache_path()) as f:
            self.assertEqual(json.load(f)['blocks'], list(T.BLOCK_NAMES))
        self.assertEqual(self._probe(), first)


class SelfRecordTest(unittest.TestCase):
    """The collector's own process tree must be easy to drop downstream. Run from
    a login shell -- `sudo ./ebpfm.sh once` for a test capture -- it inherits the
    tty and is tracked as a human, so its own `truncated` record and its
    children's (bpftool, for the BTF dump) read as somebody's work."""

    def setUp(self):
        T.proc.clear(); T._children.clear()
        self._saved = (T.writer, T.read_stat, T.FEATURES)
        T.writer = self.W = FakeWriter()
        T.read_stat = _fake_stat
        T.FEATURES = set()
        me = os.getpid()
        T.proc_add(me, 1, comm='python3', args='python3 ebpf_trace.py node0',
                   tty='pts/4', user='root', uid=0)
        T.proc_add(me + 1, me, comm='bpftool',
                   args='bpftool btf dump file /sys/kernel/btf/vmlinux -j',
                   tty='pts/4', user='root', uid=0)

    def tearDown(self):
        T.writer, T.read_stat, T.FEATURES = self._saved
        T.proc.clear(); T._children.clear()

    def test_its_own_records_are_recognisable_from_the_envelope(self):
        T.flush_live({}, set())
        recs = sorted(self.W.of('truncated'), key=lambda r: r['pid'])
        self.assertEqual([(r['command'], r['pid'] == r['collector_pid'],
                           r['ppid'] == r['collector_pid']) for r in recs],
                         [('python3', True, False), ('bpftool', False, True)])


class NetEndpointTextTest(unittest.TestCase):
    """What the harness cannot compile: the header variant needs kernel headers,
    and declaration order only exists in the assembled program."""

    def test_header_variant_reads_the_socket_through_struct_sock(self):
        txt = T.build_bpf_text({'netbytes', 'netpeer_hdr'}, offs=FAKE_OFFS)
        self.assertIn('#include <net/sock.h>', txt)
        self.assertIn('&s_->__sk_common.skc_daddr', txt)
        self.assertIn('&s_->__sk_common.skc_dport', txt)

    def test_the_birth_map_is_declared_before_the_endpoint_code_reads_it(self):
        txt = T.build_bpf_text({'netbytes', 'netpeer_btf', 'tcp_btf'}, offs=FAKE_OFFS)
        self.assertLess(txt.index('BPF_HASH(tcp_birth'), txt.index('tcp_birth.lookup'))


# ---------------------------------------------------------------------------
# schema 7: the userspace half
# ---------------------------------------------------------------------------

class FakeNetpTable(object):
    """The BCC hash `netp`, as the collector uses it: items() and `del t[k]`.
    put() sets an entry's CUMULATIVE counters, which is what the kernel holds."""

    def __init__(self):
        self.d = {}

    def put(self, tgid, ip=None, port=0, proto=6, inbound=False, tx=0, rx=0, calls=1):
        if ip is None:
            flags, addr, port = T.NETP_F_UNKNOWN, b'\x00' * 16, 0
        else:
            raw = socket.inet_pton(socket.AF_INET6 if ':' in ip else socket.AF_INET, ip)
            flags = ((T.NETP_F_V6 if len(raw) == 16 else 0)
                     | (T.NETP_F_INBOUND if inbound else 0))
            addr = (raw + b'\x00' * 16)[:16]
        k = Ev(tgid=tgid, proto=proto, flags=flags, port=port, addr=addr)
        self.d[(tgid, proto, flags, port, addr)] = (k, Ev(tx=tx, rx=rx, calls=calls))

    def items(self):
        return list(self.d.values())

    def __delitem__(self, k):
        del self.d[(k.tgid, k.proto, k.flags, k.port, bytes(k.addr))]

    def pids(self):
        return sorted({t[0] for t in self.d})


class FakeDrop(object):
    """The BCC array `netp_drop`: [calls, tx bytes, rx bytes]."""

    def __init__(self, calls=0, tx=0, rx=0):
        self.v = [calls, tx, rx]

    def __getitem__(self, i):
        return Ev(value=self.v[i])


class FakeNetb(object):
    """The BCC hash `netb` as flush_live drains it."""

    def __init__(self, d):
        self.d = d

    def items(self):
        return [(Ev(value=k), Ev(tx=t, rx=r, calls=c)) for k, (t, r, c) in self.d.items()]


def _fake_stat(pid):
    return {'cpu_ticks': 100, 'child_ticks': 0, 'threads': 1, 'state': 'S',
            'min_flt': 0, 'maj_flt': 0, 'starttime': 0, 'comm': 'curl',
            'ppid': 100, 'tty_nr': 0}


def _reset_netp_state():
    T._netp_orphans.update(entries=0, tx_bytes=0, rx_bytes=0, calls=0)
    T._netp_unclaimed.clear()
    T._netio_stats['records'] = 0


class _NetpeerCase(unittest.TestCase):
    """An agent root (100) and a curl under it (101), the endpoint table loaded."""

    def setUp(self):
        T.proc.clear(); T._children.clear()
        T._pending_exits.clear()
        self._saved = (T.writer, T.FEATURES, T._tgid_of, T.MIN_UID, T.MIN_DURATION,
                       T.MIN_CPU, T.EXIT_HOLD_MS, T._netp_tab, T._netp_drop_tab,
                       T.WANT_NETIO, T.NET_ENDPOINTS_MAX, T._COLLECTOR_START,
                       T._cidr_index, T.read_stat)
        T.writer = self.W = FakeWriter()
        T.FEATURES = {'netbytes', 'netpeer_btf'}
        T._tgid_of = lambda pid: pid
        T.MIN_UID = 0
        T.MIN_DURATION = T.MIN_CPU = 0.0
        T.EXIT_HOLD_MS = 250
        T._netp_tab = self.K = FakeNetpTable()
        T._netp_drop_tab = FakeDrop()
        T.WANT_NETIO = True
        T.NET_ENDPOINTS_MAX = 256
        T._COLLECTOR_START = 900.0
        T._cidr_index = None
        _reset_netp_state()
        T.proc_add(100, 1, comm='node', args='node /x/.claude/cli.js', user='u', uid=1000)
        T.proc_add(101, 100, comm='curl', args='curl https://api.example', user='u', uid=1000)

    def tearDown(self):
        (T.writer, T.FEATURES, T._tgid_of, T.MIN_UID, T.MIN_DURATION,
         T.MIN_CPU, T.EXIT_HOLD_MS, T._netp_tab, T._netp_drop_tab,
         T.WANT_NETIO, T.NET_ENDPOINTS_MAX, T._COLLECTOR_START,
         T._cidr_index, T.read_stat) = self._saved
        T._pending_exits.clear()
        _reset_netp_state()

    def _block(self, pid=101, tx=None, rx=None):
        return T._net_endpoints_block(T.proc[pid], tx, rx)

    def _exit(self, **kw):
        T.on_exit(_exit_ev(**kw))
        T.drain_exits(final=True)
        return self.W.of('exit')[-1]

    def _tick(self, now):
        T.netpeer_read()
        return T.netio_tick(now)


class NetpeerDecodeTest(unittest.TestCase):
    def _addr(self, ip):
        raw = socket.inet_pton(socket.AF_INET6 if ':' in ip else socket.AF_INET, ip)
        return (raw + b'\x00' * 16)[:16]

    def test_v4(self):
        self.assertEqual(T.netp_endpoint(6, 0, 443, self._addr('104.18.0.1')),
                         ('tcp', '104.18.0.1', 443, False))

    def test_v6(self):
        self.assertEqual(T.netp_endpoint(17, T.NETP_F_V6, 443, self._addr('2606:4700::1111')),
                         ('udp', '2606:4700::1111', 443, False))

    def test_v4_mapped_reads_as_v4(self):
        """A dual-stack socket names an IPv4 peer as ::ffff:a.b.c.d. Left as-is,
        one peer would show up as two endpoints."""
        self.assertEqual(T.netp_endpoint(17, T.NETP_F_V6, 443, self._addr('::ffff:1.2.3.4')),
                         ('udp', '1.2.3.4', 443, False))

    def test_inbound(self):
        self.assertEqual(T.netp_endpoint(6, T.NETP_F_INBOUND, 8888, self._addr('10.0.0.7')),
                         ('tcp', '10.0.0.7', 8888, True))

    def test_unknown_has_no_address(self):
        self.assertEqual(T.netp_endpoint(17, T.NETP_F_UNKNOWN, 0, b'\x00' * 16),
                         ('udp', None, None, None))


class NetpeerReadTest(_NetpeerCase):
    def test_the_table_lands_on_its_process(self):
        self.K.put(101, '104.18.0.1', 443, tx=1500, rx=90000, calls=40)
        self.K.put(101, '8.8.8.8', 53, proto=17, tx=40, rx=120, calls=2)
        T.netpeer_read()
        self.assertEqual([(e['proto'], e['addr'], e['port'], e['tx_bytes'], e['rx_bytes'],
                           e['calls']) for e in self._block()['endpoints']],
                         [('tcp', '104.18.0.1', 443, 1500, 90000, 40),
                          ('udp', '8.8.8.8', 53, 40, 120, 2)])

    def test_reads_replace_rather_than_add(self):
        """The kernel's counters are cumulative; adding each read would double them."""
        self.K.put(101, '104.18.0.1', 443, tx=100)
        T.netpeer_read()
        self.K.put(101, '104.18.0.1', 443, tx=250)
        T.netpeer_read()
        self.assertEqual(self._block()['endpoints'][0]['tx_bytes'], 250)

    def test_a_live_process_keeps_its_entries(self):
        """Deleting a live entry would race the kernel's increments."""
        self.K.put(101, '104.18.0.1', 443, tx=100)
        T.netpeer_read()
        T.netpeer_read()
        self.assertEqual(self.K.pids(), [101])

    def test_an_unknown_pid_gets_one_more_read_then_is_counted(self):
        """Its fork event may still be queued behind this read."""
        self.K.put(555, '104.18.0.1', 443, tx=70, rx=30, calls=3)
        T.netpeer_read()
        self.assertEqual((self.K.pids(), T._netp_orphans['entries']), ([555], 0))
        T.netpeer_read()
        self.assertEqual(self.K.pids(), [])
        self.assertEqual(T._netp_orphans,
                         {'entries': 1, 'tx_bytes': 70, 'rx_bytes': 30, 'calls': 3})

    def test_a_pid_that_shows_up_in_time_is_not_an_orphan(self):
        self.K.put(555, '104.18.0.1', 443, tx=70)
        T.netpeer_read()
        T.proc_add(555, 100, comm='git', args='git fetch', user='u', uid=1000)
        T.netpeer_read()
        self.assertEqual((self.K.pids(), T._netp_orphans['entries']), ([555], 0))
        self.assertEqual(self._block(555)['endpoints'][0]['tx_bytes'], 70)

    def test_an_exited_untracked_process_is_released_without_counting(self):
        """sshd and friends move bytes too, and are never reported by design:
        deleting their entries is housekeeping, not loss."""
        T.proc_add(900, 1, comm='sshd', args='sshd: u', user='root', uid=0)
        T.proc[900]['exited'] = True
        self.K.put(900, '10.1.1.1', 22, tx=5000)
        T.netpeer_read()
        self.assertEqual((self.K.pids(), T._netp_orphans['entries']), ([], 0))

    def test_an_exit_still_being_held_keeps_its_entries(self):
        T.on_exit(_exit_ev(net_tx=100, net_calls=1))
        self.K.put(101, '104.18.0.1', 443, tx=100)
        T.netpeer_read()
        self.assertEqual(self.K.pids(), [101])

    def test_no_table_reads_nothing(self):
        T._netp_tab = None
        self.assertIsNone(T.netpeer_read())

    def test_the_batched_lookup_is_used_where_the_kernel_has_it(self):
        """One syscall per batch instead of two per entry (5.6+)."""
        class Batched(FakeNetpTable):
            def items(self):
                raise AssertionError('per-entry iteration used despite the batch call')

            def items_lookup_batch(self):
                return iter(list(self.d.values()))
        T._netp_tab = self.K = Batched()
        self.K.put(101, '104.18.0.1', 443, tx=5)
        self.assertEqual(T.netpeer_read(), 1)
        self.assertEqual(self._block()['endpoints'][0]['tx_bytes'], 5)

    def test_a_kernel_without_batch_lookup_falls_back(self):
        class Refusing(FakeNetpTable):
            def items_lookup_batch(self):
                raise OSError(22, 'Invalid argument')     # pre-5.6: EINVAL
        T._netp_tab = self.K = Refusing()
        self.K.put(101, '104.18.0.1', 443, tx=5)
        self.assertEqual(T.netpeer_read(), 1)


class NetEndpointsBlockTest(_NetpeerCase):
    def test_an_endpoint_entry(self):
        T._cidr_index = Ev(classify=lambda ip: 'anthropic' if ip == '104.18.0.1' else None)
        self.K.put(101, '104.18.0.1', 443, tx=10, rx=20, calls=2)
        self.K.put(101, '10.0.0.5', 8888, inbound=True, rx=5, calls=1)
        T.netpeer_read()
        eps = self._block()['endpoints']
        self.assertEqual(eps[0], {'proto': 'tcp', 'addr': '104.18.0.1', 'port': 443,
                                  'host': None,
                                  'inbound': False, 'external': True, 'provider': 'anthropic',
                                  'tx_bytes': 10, 'rx_bytes': 20, 'calls': 2})
        self.assertEqual((eps[1]['inbound'], eps[1]['external'], eps[1]['provider']),
                         (True, False, None))

    def test_complete_and_sorted_by_bytes(self):
        for i, n in enumerate((5, 500, 50)):
            self.K.put(101, '104.18.0.%d' % (i + 1), 443, rx=n)
        T.netpeer_read()
        blk = self._block()
        self.assertEqual([e['rx_bytes'] for e in blk['endpoints']], [500, 50, 5])
        self.assertEqual(blk['n'], 3)
        self.assertNotIn('overflow', blk)

    def test_past_the_cap_the_rest_is_counted_not_dropped(self):
        T.NET_ENDPOINTS_MAX = 2
        for i, n in enumerate((5, 500, 50)):
            self.K.put(101, '104.18.0.%d' % (i + 1), 443, rx=n, calls=1)
        T.netpeer_read()
        blk = self._block()
        self.assertEqual([e['rx_bytes'] for e in blk['endpoints']], [500, 50])
        self.assertEqual(blk['n'], 3)
        self.assertEqual(blk['overflow'], {'n': 1, 'tx_bytes': 0, 'rx_bytes': 5, 'calls': 1})

    def test_v4_mapped_and_plain_v4_are_one_endpoint(self):
        self.K.put(101, '::ffff:1.2.3.4', 443, proto=17, tx=10)
        self.K.put(101, '1.2.3.4', 443, proto=17, tx=5)
        T.netpeer_read()
        self.assertEqual([(e['addr'], e['tx_bytes'], e['calls']) for e in self._block()['endpoints']],
                         [('1.2.3.4', 15, 2)])

    def test_unattributed_bytes_have_their_own_bucket(self):
        self.K.put(101, None, proto=17, rx=64, calls=1)
        self.K.put(101, '8.8.8.8', 53, proto=17, rx=100, calls=1)
        T.netpeer_read()
        blk = self._block()
        self.assertEqual(blk['unattributed'], {'tx_bytes': 0, 'rx_bytes': 64, 'calls': 1})
        self.assertEqual((blk['n'], len(blk['endpoints'])), (1, 1))

    def test_null_for_a_process_with_no_network_io(self):
        self.assertIsNone(self._block(tx=0, rx=0))

    def test_null_when_the_block_did_not_load(self):
        T.FEATURES = {'netbytes'}
        self.K.put(101, '104.18.0.1', 443, tx=10)
        T.netpeer_read()
        self.assertIsNone(self._block(tx=10, rx=0))

    def test_bytes_the_table_missed_are_unaccounted_not_hidden(self):
        """A full table, or a call already in flight at attach, reaches the totals
        but not the endpoint table; the gap is stated."""
        self.K.put(101, '104.18.0.1', 443, tx=100, rx=50)
        T.netpeer_read()
        self.assertEqual(self._block(tx=130, rx=50)['unaccounted'],
                         {'tx_bytes': 30, 'rx_bytes': 0})

    def test_totals_with_no_entry_at_all_are_all_unaccounted(self):
        blk = self._block(tx=40, rx=0)
        self.assertEqual((blk['n'], blk['endpoints'], blk['unaccounted']),
                         (0, [], {'tx_bytes': 40, 'rx_bytes': 0}))

    def test_an_endpoint_the_command_line_names_carries_that_name(self):
        T.proc[101]['args'] = 'curl -s https://api.example.org/v1'
        T._HOST_CACHE['api.example.org'] = ({'93.184.216.34'}, float('inf'))
        try:
            self.K.put(101, '93.184.216.34', 443, tx=10)
            self.K.put(101, '140.82.112.3', 443, tx=5)
            T.netpeer_read()
            self.assertEqual({e['addr']: e['host'] for e in self._block()['endpoints']},
                             {'93.184.216.34': 'api.example.org', '140.82.112.3': None})
            self.assertTrue(T.netio_tick(1000.0))
            self.assertEqual({e['addr']: e['host'] for e in self.W.of('netio')[0]['endpoints']},
                             {'93.184.216.34': 'api.example.org', '140.82.112.3': None})
        finally:
            T._HOST_CACHE.clear()

    def test_a_name_not_yet_resolved_labels_nothing(self):
        T.proc[101]['args'] = 'curl -s https://api.example.org/v1'
        self.K.put(101, '93.184.216.34', 443, tx=10)
        T.netpeer_read()
        self.assertIsNone(self._block()['endpoints'][0]['host'])

    def test_no_unaccounted_key_when_everything_reconciles(self):
        self.K.put(101, '104.18.0.1', 443, tx=100, rx=50)
        T.netpeer_read()
        self.assertNotIn('unaccounted', self._block(tx=100, rx=50))


class ArgvHostnamesTest(unittest.TestCase):
    """Hostnames a process's own command line names. Nothing here reads
    traffic: the source is argv, which the collector already records."""

    def _h(self, args):
        return T._argv_hostnames(args, os.path.basename(args.split(' ', 1)[0]))

    def test_a_url(self):
        self.assertEqual(self._h('curl -s https://api.github.com/repos/x/y'), ['api.github.com'])

    def test_credentials_in_a_url_never_come_along(self):
        self.assertEqual(self._h('git clone https://u:ghp_token@github.com/x/y.git'),
                         ['github.com'])

    def test_a_url_inside_an_option_with_a_port(self):
        self.assertEqual(self._h('pip install --index-url=https://pypi.example.org:8443/simple x'),
                         ['pypi.example.org'])

    def test_several_urls_in_order_without_repeats(self):
        self.assertEqual(self._h('wget https://a.example.org/1 http://B.example.org/2 '
                                 'https://a.example.org/3'),
                         ['a.example.org', 'b.example.org'])

    def test_the_ssh_destination_after_its_options(self):
        self.assertEqual(self._h('ssh -p 2222 -i /home/u/.ssh/id_ed25519 -o BatchMode=yes '
                                 'u@login.example.org uptime'),
                         ['login.example.org'])

    def test_the_ssh_that_git_spawns_for_a_remote(self):
        self.assertEqual(self._h("ssh -x git@github.com git-upload-pack 'org/repo.git'"),
                         ['github.com'])

    def test_scp_and_rsync_host_paths(self):
        self.assertEqual(self._h('scp -P 22 notes.txt u@dtn.example.org:/scratch/u/'),
                         ['dtn.example.org'])
        self.assertEqual(self._h('rsync -av ./out backup.example.org::share'),
                         ['backup.example.org'])

    def test_no_hostname_is_invented(self):
        """File names and email addresses look like hostnames; they must never
        become DNS lookups."""
        for args in ('python3 train.py --out model.pt',
                     'git config user.email me@example.org',
                     'ls -la /home/u', 'node /x/.claude/cli.js'):
            self.assertEqual(self._h(args), [], args)

    def test_ip_literals_and_localhost_need_no_name(self):
        self.assertEqual(self._h('curl http://127.0.0.1:8080/ http://[::1]/ http://10.0.0.5/ '
                                 'http://[2001:db8::a]/ http://localhost:3000/'), [])


def _drain(q):
    while not q.empty():
        q.get_nowait()


class ArgvHostResolverTest(unittest.TestCase):
    """The lookups run on a background thread, so they are driven here one step
    at a time: _want_host() queues, _resolve_host() is what the thread does."""

    def setUp(self):
        self._gai = T._host_getaddrinfo
        T._HOST_CACHE.clear()
        T._host_pending.clear()
        _drain(T._HOST_WANT)
        T._host_stats.update(resolved=0, failed=0, dropped=0)
        self.calls = []

        def fake(name, *a, **kw):
            self.calls.append(name)
            if name == 'api.example.org':
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0)),
                        (socket.AF_INET6, socket.SOCK_STREAM, 6, '',
                         ('2606:2800:220:1:248:1893:25c8:1946', 0, 0, 0))]
            raise socket.gaierror(-2, 'Name or service not known')
        T._host_getaddrinfo = fake

    def tearDown(self):
        T._host_getaddrinfo = self._gai
        T._HOST_CACHE.clear()
        T._host_pending.clear()
        _drain(T._HOST_WANT)

    def test_a_wanted_name_is_resolved_once(self):
        T._want_host('api.example.org')
        T._want_host('api.example.org')
        self.assertEqual(T._HOST_WANT.qsize(), 1)
        T._resolve_host(T._HOST_WANT.get_nowait())
        self.assertEqual(T._host_ips('api.example.org'),
                         {'93.184.216.34', '2606:2800:220:1:248:1893:25c8:1946'})
        T._want_host('api.example.org')             # cached now
        self.assertEqual(T._HOST_WANT.qsize(), 0)
        self.assertEqual(self.calls, ['api.example.org'])

    def test_a_name_that_does_not_resolve_is_remembered_as_such(self):
        T._resolve_host('nope.example.org')
        self.assertEqual(T._host_ips('nope.example.org'), set())
        T._want_host('nope.example.org')
        self.assertEqual(T._HOST_WANT.qsize(), 0)
        self.assertEqual(T._host_stats['failed'], 1)

    def test_a_full_queue_is_counted_and_the_name_can_be_asked_again(self):
        for i in range(T._HOST_WANT.maxsize + 3):
            T._want_host('h%d.example.org' % i)
        self.assertEqual(T._host_stats['dropped'], 3)
        _drain(T._HOST_WANT)
        T._want_host('h%d.example.org' % (T._HOST_WANT.maxsize + 2))
        self.assertEqual(T._HOST_WANT.qsize(), 1)

    def test_an_exec_with_a_url_asks_for_its_name(self):
        T.proc.clear(); T._children.clear()
        T.proc_add(100, 1, comm='node', args='node /x/.claude/cli.js', user='u', uid=1000)
        T.proc_add(101, 100, comm='curl', user='u', uid=1000)
        T.on_argv(_argv_ev(101, b'curl\x00-s\x00https://api.example.org/v1\x00'))
        self.assertEqual(T._HOST_WANT.get_nowait(), 'api.example.org')

    def test_an_untracked_process_asks_for_nothing(self):
        """Names are looked up only for processes the collector reports."""
        T.proc.clear(); T._children.clear()
        T.proc_add(900, 1, comm='curl', user='root', uid=0)
        T.on_argv(_argv_ev(900, b'curl\x00https://updates.example.org/x\x00'))
        self.assertEqual(T._HOST_WANT.qsize(), 0)


class NetEndpointsExitTest(_NetpeerCase):
    def test_the_exit_record_carries_the_endpoint_list(self):
        self.K.put(101, '104.18.0.1', 443, tx=1500, rx=9000, calls=12)
        rec = self._exit(net_tx=1500, net_rx=9000, net_calls=12)
        blk = rec['net_endpoints']
        self.assertEqual([(e['addr'], e['tx_bytes'], e['rx_bytes']) for e in blk['endpoints']],
                         [('104.18.0.1', 1500, 9000)])
        self.assertFalse(blk['in_series'])
        self.assertNotIn('unaccounted', blk)

    def test_the_table_is_read_again_when_the_record_is_written(self):
        self.K.put(101, '104.18.0.1', 443, tx=100)
        T.netpeer_read()                              # an earlier tick saw part of it
        self.K.put(101, '104.18.0.1', 443, tx=300)    # the rest arrived before exit
        rec = self._exit(net_tx=300, net_calls=3)
        self.assertEqual(rec['net_endpoints']['endpoints'][0]['tx_bytes'], 300)

    def test_a_written_process_releases_its_entries(self):
        self.K.put(101, '104.18.0.1', 443, tx=100)
        self._exit(net_tx=100, net_calls=1)
        self.assertEqual((self.K.pids(), T._netp_orphans['entries']), ([], 0))

    def test_bytes_after_the_record_are_counted_as_orphans(self):
        """A thread that outlived the group leader keeps sending; those bytes can
        no longer reach a record."""
        self.K.put(101, '104.18.0.1', 443, tx=100)
        self._exit(net_tx=100, net_calls=1)
        self.K.put(101, '104.18.0.1', 443, tx=7, calls=1)
        T.netpeer_read()
        self.assertEqual(self.K.pids(), [])
        self.assertEqual((T._netp_orphans['entries'], T._netp_orphans['tx_bytes']), (1, 7))

    def test_a_short_udp_only_process_is_kept(self):
        """MIN_DURATION would drop a 3 ms DNS lookup. A process that moved bytes is
        never a no-op, TCP connection or not."""
        T.MIN_DURATION = 10.0
        self.K.put(101, '8.8.8.8', 53, proto=17, tx=40, rx=120, calls=2)
        rec = self._exit(run_ns=int(3e6), net_tx=40, net_rx=120, net_calls=2)
        self.assertEqual(rec['net_endpoints']['endpoints'][0]['port'], 53)

    def test_a_short_process_with_no_network_is_still_dropped(self):
        T.MIN_DURATION = 10.0
        T.on_exit(_exit_ev(run_ns=int(3e6)))
        T.drain_exits(final=True)
        self.assertEqual(self.W.of('exit'), [])


class NetioSeriesTest(_NetpeerCase):
    def test_the_first_tick_with_traffic_starts_the_series(self):
        self.K.put(101, '104.18.0.1', 443, tx=500, calls=5)
        self.assertEqual(self._tick(1000.0), 1)
        (r,) = self.W.of('netio')
        self.assertEqual((r['pid'], r['actor3'], r['reason'], r['interval_s']),
                         (101, 'agent', 'tick', 100.0))
        self.assertEqual([(e['addr'], e['tx_bytes'], e['calls']) for e in r['endpoints']],
                         [('104.18.0.1', 500, 5)])

    def test_a_quiet_interval_writes_nothing(self):
        self.K.put(101, '104.18.0.1', 443, tx=500)
        self._tick(1000.0)
        self.assertEqual(self._tick(1060.0), 0)
        self.assertEqual(len(self.W.of('netio')), 1)

    def test_each_record_carries_only_its_interval(self):
        self.K.put(101, '104.18.0.1', 443, tx=500, calls=5)
        self._tick(1000.0)
        self.K.put(101, '104.18.0.1', 443, tx=800, calls=7)
        self.K.put(101, '140.82.112.3', 443, rx=64, calls=1)
        self._tick(1060.0)
        r = self.W.of('netio')[1]
        self.assertEqual(r['interval_s'], 60.0)
        self.assertEqual(sorted((e['addr'], e['tx_bytes'], e['rx_bytes'], e['calls'])
                                for e in r['endpoints']),
                         [('104.18.0.1', 300, 0, 2), ('140.82.112.3', 0, 64, 1)])

    def test_the_exit_writes_the_remainder_and_says_so(self):
        self.K.put(101, '104.18.0.1', 443, tx=500)
        self._tick(1000.0)
        self.K.put(101, '104.18.0.1', 443, tx=800)
        self._tick(1060.0)
        self.K.put(101, '104.18.0.1', 443, tx=1000)
        rec = self._exit(net_tx=1000, net_calls=3)
        final = self.W.of('netio')[-1]
        self.assertEqual((final['reason'], final['endpoints'][0]['tx_bytes']), ('exit', 200))
        self.assertTrue(rec['net_endpoints']['in_series'])
        self.assertEqual(rec['net_endpoints']['endpoints'][0]['tx_bytes'], 1000)

    def test_the_series_alone_accounts_for_every_byte_of_a_series_process(self):
        """The consumer rule: sum the netio records, add exit records whose
        in_series is false. For a series process that must be its lifetime."""
        for tx in (500, 800):
            self.K.put(101, '104.18.0.1', 443, tx=tx)
            self._tick(1000.0 + tx)
        self.K.put(101, '104.18.0.1', 443, tx=1000)
        self._exit(net_tx=1000, net_calls=3)
        self.assertEqual(sum(e['tx_bytes'] for r in self.W.of('netio') for e in r['endpoints']),
                         1000)

    def test_no_remainder_means_no_final_record(self):
        self.K.put(101, '104.18.0.1', 443, tx=500)
        self._tick(1000.0)
        self._exit(net_tx=500, net_calls=1)
        self.assertEqual([r['reason'] for r in self.W.of('netio')], ['tick'])

    def test_a_process_never_seen_at_a_tick_is_only_in_its_exit_record(self):
        self.K.put(101, '104.18.0.1', 443, tx=500)
        rec = self._exit(net_tx=500, net_calls=1)
        self.assertEqual(self.W.of('netio'), [])
        self.assertFalse(rec['net_endpoints']['in_series'])

    def test_untracked_processes_are_not_in_the_series(self):
        T.proc_add(900, 1, comm='sshd', args='sshd: u', user='root', uid=0)
        self.K.put(900, '10.1.1.1', 22, tx=5000)
        self.assertEqual(self._tick(1000.0), 0)
        self.assertEqual(self.W.of('netio'), [])

    def test_the_series_can_be_switched_off(self):
        T.WANT_NETIO = False
        self.K.put(101, '104.18.0.1', 443, tx=500)
        self.assertIsNone(self._tick(1000.0))
        rec = self._exit(net_tx=500, net_calls=1)
        self.assertEqual(self.W.of('netio'), [])
        self.assertFalse(rec['net_endpoints']['in_series'])

    def test_stop_writes_the_remainder_of_live_processes(self):
        self.K.put(101, '104.18.0.1', 443, tx=500)
        self._tick(1000.0)
        self.K.put(101, '104.18.0.1', 443, tx=650)
        T.netpeer_read()
        self.assertEqual(T.netio_final_live(1030.0), 1)
        final = self.W.of('netio')[-1]
        self.assertEqual((final['reason'], final['endpoints'][0]['tx_bytes'], final['interval_s']),
                         ('stop', 150, 30.0))

    def test_truncated_records_carry_the_block(self):
        self.K.put(101, '104.18.0.1', 443, tx=500)
        self._tick(1000.0)
        T.read_stat = _fake_stat
        T.flush_live({'netb': FakeNetb({101: (500, 0, 5)})}, T.FEATURES)
        (tr,) = [r for r in self.W.of('truncated') if r['pid'] == 101]
        self.assertTrue(tr['net_endpoints']['in_series'])
        self.assertEqual(tr['net_endpoints']['endpoints'][0]['tx_bytes'], 500)


class NetpeerTotalsTest(_NetpeerCase):
    def setUp(self):
        super(NetpeerTotalsTest, self).setUp()
        T.read_stat = _fake_stat

    def test_residency_totals_report_the_table(self):
        T._netp_drop_tab = FakeDrop(calls=2, tx=300)
        self.K.put(101, '104.18.0.1', 443, tx=500)
        self.K.put(555, '1.1.1.1', 443, tx=1)       # not known yet: one more read
        T.residency_tick(None)
        tot = self.W.of('residency_totals')[-1]
        self.assertEqual((tot['netio_records'], tot['netpeer_entries']), (1, 2))
        self.assertEqual(tot['netpeer_full'], {'calls': 2, 'tx_bytes': 300, 'rx_bytes': 0})
        self.assertEqual(tot['netpeer_orphans'],
                         {'entries': 0, 'tx_bytes': 0, 'rx_bytes': 0, 'calls': 0})
        self.assertEqual([r['pid'] for r in self.W.of('netio')], [101])

    def test_null_not_zero_when_the_block_is_absent(self):
        T.FEATURES = {'netbytes'}
        T._netp_tab = T._netp_drop_tab = None
        T.residency_tick(None)
        tot = self.W.of('residency_totals')[-1]
        for k in ('netio_records', 'netpeer_entries', 'netpeer_full', 'netpeer_orphans'):
            self.assertIsNone(tot[k], k)


# ---------------------------------------------------------------------------
# conn records: the protocol the netlink row came from
# ---------------------------------------------------------------------------

def _birth_ev(pid=101, saddr='10.0.0.2', sport=40000, daddr='104.18.0.1', dport=443):
    return Ev(family=2, saddr_v4=int.from_bytes(socket.inet_aton(saddr), 'little'),
              daddr_v4=int.from_bytes(socket.inet_aton(daddr), 'little'),
              saddr_v6=0, daddr_v6=0, sport=sport, dport=dport, pid=pid, uid=1000,
              comm=b'node', inbound=0, ts=1, estab_ts=2)


def _diag_row(proto, daddr='128.103.1.210', dport=67, sport=68, info=None):
    """A row exactly as sockdiag.parse_messages() builds one, plus its protocol."""
    return {'family': 'inet', 'state': 'ESTABLISHED', 'state_num': 1, 'retrans_now': 0,
            'timer': 0, 'saddr': '10.0.0.2', 'sport': sport, 'daddr': daddr, 'dport': dport,
            'rqueue': 0, 'wqueue': 0, 'uid': 997, 'inode': 1, 'info': info, 'proto': proto}


class ConnProtoTest(unittest.TestCase):
    """The netlink dump asks for TCP and UDP, and a connected UDP socket reports
    ESTABLISHED. With no protocol on the row, a DHCP client's socket read as a
    standing TCP connection held by nobody: 1,439 such `conn` records a day on
    one host."""

    def setUp(self):
        T.proc.clear(); T._children.clear()
        self._saved = (T.FEATURES, T.sockdiag.dump, T.CONN_ALL, T.CONN_UNTRACKED)
        T.FEATURES = {'tcp_btf'}
        T.CONN_ALL, T.CONN_UNTRACKED = False, True

    def tearDown(self):
        T.FEATURES, T.sockdiag.dump, T.CONN_ALL, T.CONN_UNTRACKED = self._saved

    def _tick(self, rows, births=()):
        T.sockdiag.dump = lambda: list(rows)
        b = {'tcp_birth': Ev(items=lambda: [(Ev(value=i), v) for i, v in enumerate(births)])}
        return T.conn_tick(b)['records']

    def test_parse_stamps_the_protocol_it_was_asked_for(self):
        self.assertEqual(sockdiag.parse_messages(_mk_msg(info=None), proto='udp')[0]['proto'],
                         'udp')

    def test_a_udp_socket_is_labelled_udp(self):
        (rec,) = self._tick([_diag_row('udp')])
        self.assertEqual((rec['proto'], rec['daddr'], rec['dport']), ('udp', '128.103.1.210', 67))

    def test_a_udp_row_never_joins_a_tcp_birth(self):
        """Same four-tuple, different protocol: the TCP connection's stats must not
        come from the UDP socket."""
        recs = self._tick([_diag_row('udp', daddr='104.18.0.1', dport=443, sport=40000)],
                          [_birth_ev()])
        by = {r['proto']: r for r in recs}
        self.assertEqual(set(by), {'tcp', 'udp'})
        self.assertIsNone(by['tcp']['stats_src'])
        self.assertEqual(by['udp']['stats_src'], 'netlink')

    def test_a_tcp_row_still_joins_its_birth(self):
        recs = self._tick([_diag_row('tcp', daddr='104.18.0.1', dport=443, sport=40000,
                                     info={'rtt_us': 31200})], [_birth_ev()])
        self.assertEqual([(r['proto'], r['stats_src'], r['rtt_ms']) for r in recs],
                         [('tcp', 'netlink', 31.2)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
