"""Unit tests for eBPF_marthen_new's userspace logic.

Runs anywhere: no bcc, no root, no kernel. Everything here is either a pure
function or the process-table logic driven by synthetic events, which is where
the bugs that a live capture cannot easily reveal actually live.

    python3 -m unittest discover eBPF_marthen_new/tests
"""
import os
import socket
import struct
import sys
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
                   '__TCP_BYTES__'):
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
        T._dropped['comms'] = {}
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
        self.assertEqual(T.proc[104]['cwd_source'], 'inherit')

    def test_cwd_inherit_does_not_overwrite_a_read(self):
        T.proc_add(105, 100, comm='git', args='git log', user='u', uid=1000,
                   cwd='/tmp/elsewhere', cwd_source='proc')
        T._inherit_cwd(T.proc[105])
        self.assertEqual(T.proc[105]['cwd'], '/tmp/elsewhere')
        self.assertEqual(T.proc[105]['cwd_source'], 'proc')

    def test_cwd_absent_when_parent_has_none(self):
        T.proc_add(201, 200, comm='ls', args='ls', user='h', uid=1004)
        T._inherit_cwd(T.proc[201])
        self.assertIsNone(T.proc[201]['cwd'])

    def test_identity_carries_cwd(self):
        ident = T._base_identity(T.proc[100], 'agent', 'ancestry')
        self.assertEqual(ident['cwd'], '/home/u/proj')
        self.assertIn('cwd_source', ident)

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
        self.assertEqual(m['cwd_source'], 'inherit')
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
        self.assertEqual(T._dropped['comms']['systemd-udevd'], 1)


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
        e = T._envelope('exit')
        self.assertEqual(set(e), {'ts', 'ts_epoch', 'host', 'event',
                                  'source', 'collector', 'schema_version'})
        self.assertIsInstance(e['ts_epoch'], float)
        self.assertEqual(e['event'], 'exit')

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
        self.assertEqual((st['pid'], st['uid'], st['comm']), (5, 1000, 'curl'))
        self.assertIsNone(st['actor'])
        self.assertIsNone(st['agent_pid'])
        self.assertIsNone(st['cgroup'])
        # present-and-null, never absent
        for k in ('ppid', 'tty', 'args', 'cwd', 'session_key', 'tree_root_pid'):
            self.assertIn(k, st)
            self.assertIsNone(st[k])

    def test_schema_version_is_pinned(self):
        """Never asserted before. A bump is a deliberate act with a downstream
        cost, not something that should drift in unnoticed."""
        self.assertEqual(T.SCHEMA_VERSION, 5)
        self.assertEqual(T.COLLECTOR, 'ebpf_marthen_new')

    def test_top_n(self):
        self.assertEqual(T._top_n({'a': 3, 'b': 9, 'c': 1}, 2), {'b': 9, 'a': 3})
        self.assertEqual(T._top_n({}, 5), {})
        self.assertIsNone(T._top_n({}, 5, or_none=True))


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
                  'approval_mode', 'approval_src', 'env_flags'):
            self.assertIn(k, ident)


class ArgsTruncationTest(unittest.TestCase):
    def setUp(self):
        T.proc.clear(); T._children.clear()

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

    def test_flag_true_past_the_emit_cap(self):
        T.proc_add(701, 1, comm='sh', args='x' * 9000)
        T.proc[701]['args_raw_len'] = 9000       # exceeds ARGV_KMAX too
        i = T._base_identity(T.proc[701], 'human', 'tty')
        self.assertEqual(i['args_len'], 9000)
        self.assertTrue(i['args_truncated'])
        self.assertEqual(len(i['args']), T.ARGS_MAXLEN)

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
