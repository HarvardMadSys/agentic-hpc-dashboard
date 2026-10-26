#!/usr/bin/env python3
"""Compile the generated BPF program with BCC, without root.

Loading needs root, and so does the tracefs read BCC uses to build each
TRACEPOINT_PROBE's argument struct. Compiling needs neither: this supplies those
structs itself, runs BCC's real frontend (the rewriter included) and LLVM, and
reports what they reject -- syntax and type errors, a rewriter that cannot follow
a pointer, the 512-byte stack limit. The map creation that follows fails without
privilege, and that failure is the expected end of a clean compile.

What it cannot show is the verifier's verdict, which only a load gives.

    python3 tests/bpf_compile_check.py                 # every block alone, then all together
    python3 tests/bpf_compile_check.py signal,io,...   # one feature set

Exit status 0 when every set compiled. Run from collector/ or anywhere: it finds
ebpf_trace.py next to its own directory. Needs python3-bcc and kernel headers.
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('EBPFM_OUTPUT_DIR', tempfile.mkdtemp(prefix='ebpfm_cc_'))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'lib'))

import ebpf_trace as T          # noqa: E402

# Field layouts as the kernel's format files give them (4.18 and 6.x agree on
# every field the program reads). The leading 8 bytes are the common header.
TP_STRUCTS = r'''
struct tracepoint__sched__sched_process_fork {
    u64 __hdr; char parent_comm[16]; pid_t parent_pid; char child_comm[16]; pid_t child_pid;
};
struct tracepoint__sched__sched_process_exec {
    u64 __hdr; int __data_loc_filename; pid_t pid; pid_t old_pid;
};
struct tracepoint__sched__sched_process_exit {
    u64 __hdr; char comm[16]; pid_t pid; int prio;
};
struct tracepoint__sched__sched_stat_blocked {
    u64 __hdr; char comm[16]; pid_t pid; u64 delay;
};
struct tracepoint__syscalls__sys_enter_write {
    u64 __hdr; int __syscall_nr; u64 fd; const char *buf; u64 count;
};
struct tracepoint__sock__inet_sock_set_state {
    u64 __hdr; const void *skaddr; int oldstate; int newstate; u16 sport; u16 dport;
    u16 family; u16 protocol; u8 saddr[4]; u8 daddr[4]; u8 saddr_v6[16]; u8 daddr_v6[16];
};
'''

# offsets a 6.x kernel's BTF gives; only their presence matters to a compile
FAKE_OFFS = {
    'tcp_sock': {'bytes_received': 1784, 'bytes_acked': 1840},
    'sock_common': {'skc_family': 16, 'skc_rcv_saddr': 4, 'skc_daddr': 0,
                    'skc_num': 14, 'skc_dport': 12,
                    'skc_v6_rcv_saddr': 72, 'skc_v6_daddr': 56},
    'sock': {'sk_socket': 288},
    'socket': {'file': 16},
}


class _Fd2(object):
    """BCC's clang writes to fd 2 directly; collect it."""
    def __enter__(self):
        sys.stderr.flush()
        self.saved = os.dup(2)
        self.tmp = tempfile.TemporaryFile()
        os.dup2(self.tmp.fileno(), 2)
        return self

    def __exit__(self, *a):
        os.dup2(self.saved, 2)
        os.close(self.saved)
        self.tmp.seek(0)
        self.text = self.tmp.read().decode('utf-8', 'replace')
        self.tmp.close()
        return False


def compile_set(feats, offs=FAKE_OFFS):
    """(ok, diagnostics) for one feature set."""
    from bcc import BPF
    text = TP_STRUCTS + T.build_bpf_text(set(feats), offs)
    cap = _Fd2()
    err = None
    with cap:
        try:
            BPF(text=text, cflags=T.CFLAGS)
        except Exception as e:          # expected: the maps cannot be created
            err = str(e)
    lines = [ln for ln in cap.text.splitlines()
             if 'error' in ln.lower() and 'could not open bpf map' not in ln.lower()]
    if 'stack limit' in cap.text.lower():
        lines.append('BPF stack limit exceeded')
    if lines:
        return False, '\n'.join(lines[:12])
    if err is not None and 'could not open bpf map' not in cap.text.lower():
        return False, err            # failed for some other reason
    return True, ''


def greedy():
    """probe_features() without the load: add each block in BLOCKS order when
    it compiles together with everything already accepted. A variant written for
    another kernel's headers (pids_task, tcp_hdr, netpeer_hdr on 6.x) fails here
    exactly as it would be dropped there, so the result is this host's set and
    a FAIL line is only a defect when no variant of its group compiled."""
    accepted = set()
    report = []
    for name, requires, _purpose in T.BLOCKS:
        alt = T.ALTERNATIVES.get(name)
        sup = [a for a in ((alt,) if alt else ()) + T.ALSO_SUPERSEDED_BY.get(name, ())
               if a in accepted]
        if sup or any(r not in accepted for r in requires):
            report.append((name, None, 'skipped'))
            continue
        ok, diag = compile_set(accepted | {name})
        if ok:
            accepted.add(name)
        report.append((name, ok, diag))
    return accepted, report


def main(argv):
    if argv:
        bad = 0
        for a in argv:
            feats = {x for x in a.split(',') if x}
            ok, diag = compile_set(feats)
            print('%-4s %s' % ('ok' if ok else 'FAIL', ','.join(sorted(feats))))
            if not ok:
                bad += 1
                print('     ' + diag.replace('\n', '\n     '))
        return 1 if bad else 0
    ok, diag = compile_set(set())
    print('%-4s %s' % ('ok' if ok else 'FAIL', 'base'))
    if not ok:
        print('     ' + diag.replace('\n', '\n     '))
        return 1
    accepted, report = greedy()
    for name, ok, diag in report:
        print('%-4s %-14s %s' % ({True: 'ok', False: 'FAIL', None: '--'}[ok], name,
                                 diag.split('\n', 1)[0] if diag else ''))
    groups = [T.PIDS_BLOCKS, T.TCP_BLOCKS, T.NETPEER_BLOCKS, {'argv_user', 'argv_kernel'}]
    loners = set(T.BLOCK_NAMES) - set().union(*groups)
    gaps = [n for n in sorted(loners) if n not in accepted]
    gaps += ['/'.join(sorted(g)) for g in groups if not (g & accepted)]
    print('accepted: %s' % ','.join(sorted(accepted)))
    if gaps:
        print('no compiling variant: %s' % ', '.join(gaps))
    return 1 if gaps else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
