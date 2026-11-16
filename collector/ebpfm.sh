#!/usr/bin/env bash
# ebpfm.sh — the only command you need for the login-node collector.
#
# Collects in two tiers, chosen by privilege, because only one of them needs root:
#
#   node   UNPRIVILEGED. Node health: load, memory, filesystem space, NFS RTT,
#          interface counters, D-state census, process tree, per-user cgroup
#          totals, boot time. eBPF cannot produce any of it.
#   ebpf   ROOT. Every process exit with its status, argv, cwd, all-user I/O,
#          D-state dwell, network bytes, TCP records. No unprivileged substitute.
#
# Run it as root and you get both tiers. Run it as an ordinary user and you get
# the node tier, with `check` naming exactly what is skipped. EBPFM_TIER
# (node|ebpf|all) overrides the choice. Both tiers write their day-files as
# JSONL, as EBR (lib/ebr.py's binary records, the same records 4-8x smaller), or
# as both, twins: EBPFM_FORMAT=jsonl|ebr|both, default both.
#
#        ./ebpfm.sh check          one line per precondition; exit code = FAIL count
#        ./ebpfm.sh start|stop|status
#        ./ebpfm.sh once [secs]    foreground capture
#        ./ebpfm.sh tail [n]       last n records of today's file
#        ./ebpfm.sh compare [day]  JSONL vs EBR twins, record by record, both tiers
#        ./ebpfm.sh verify [day]   check every frame and record of the day's EBR files
#        ./ebpfm.sh bench [file]   EBR's size and codec speed on a day-file, in memory
#        ./ebpfm.sh ebr VERB ...   lib/ebr.py: cat convert verify compare count append bench
#        ./ebpfm.sh bundle [out]   emit a self-extracting single-file copy
#   sudo ./ebpfm.sh bootstrap      install dependencies, set required sysctls, verify
#   sudo ./ebpfm.sh features       probe the BPF blocks, print the resolved set
#   sudo ./ebpfm.sh overhead       one-off cost measurement (see README)
#   sudo ./ebpfm.sh install-unit   install the persistent systemd unit (fleet deploys)
#
# Why `start` uses systemd-run: the userspace drain is cgroup-charged, and `sudo`
# does NOT leave the caller's slice — a sudo'd child stays in
# /user.slice/user-N.slice/session-M.scope. On a login node that slice is capped
# at cpu.max = 1.0 CPU, so a sudo-launched collector would share one CPU with
# everything else the user runs, which is the bottleneck this collector exists to
# escape. system.slice has no such cap.
set -uo pipefail

# Through any symlink: the RPM links /usr/bin/ebpfm here, and the tracer and
# node tier live next to the real file, not next to the link.
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# Absolute path to THIS script. $0 is whatever the caller typed: invoked as
# `bash ebpfm.sh start` it is the bare relative "ebpfm.sh", which nohup then
# looks up on PATH, not in cwd, so the node-tier child dies instantly with
# "failed to run command". Resolve once and use $SELF everywhere $0 was meant
# to mean "this file" -- re-exec, the bundle self-extractor, and the printed
# hints, which become copy-pasteable as a side effect.
SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"
TRACER="$DIR/ebpf_trace.py"
NODE_SNAPSHOT="$DIR/node_snapshot.py"
EBR="$DIR/lib/ebr.py"
NAME="ebpfm"
HOST="$(hostname -s)"
IS_ROOT=0; [ "$(id -u)" = 0 ] && IS_ROOT=1

# Defaults differ by privilege: an unprivileged run can write neither /var/log
# nor /run, and the node tier is meant to work unprivileged.
if [ "$IS_ROOT" = 1 ]; then
    # Prefer /scratch: the collector's JSONL is unbounded and these login nodes
    # have a small root disk, while /scratch is large and root-writable. Fall
    # back to /var/log/ebpfm where /scratch is absent (dev boxes, CI, other
    # sites). The systemd unit sets EBPFM_OUTPUT_DIR explicitly, so this only
    # governs a manual `ebpfm.sh start`/`once` by root.
    if [ -d /scratch ] && [ -w /scratch ]; then
        _DEF_OUT=/scratch/ebpfm;      _DEF_NODE=/scratch/ebpfm-login;   _DEF_PID=/run
    else
        _DEF_OUT=/var/log/ebpfm;      _DEF_NODE=/var/log/ebpfm-login;   _DEF_PID=/run
    fi
else
    _DEF_OUT="$HOME/.ebpfm/ebpf";     _DEF_NODE="$HOME/.ebpfm/login";   _DEF_PID="$HOME/.run"
fi
OUTPUT_DIR="${EBPFM_OUTPUT_DIR:-$_DEF_OUT}"
NODE_OUTPUT_DIR="${EBPFM_NODE_OUTPUT_DIR:-$_DEF_NODE}"
PID_DIR="${EBPFM_PID_DIR:-$_DEF_PID}"
PID_FILE="$PID_DIR/${NAME}_${HOST}.pid"
NODE_PID_FILE="$PID_DIR/${NAME}-node_${HOST}.pid"
LOG="$OUTPUT_DIR/${NAME}_${HOST}.log"
NODE_LOG="$NODE_OUTPUT_DIR/${NAME}-node_${HOST}.log"
NODE_INTERVAL="${EBPFM_NODE_INTERVAL:-300}"
NODE_TOP_N="${EBPFM_NODE_TOP_N:-25}"
export EBPFM_OUTPUT_DIR="$OUTPUT_DIR"

# Output permissions. The collected JSONL is sensitive (argv, cwd, all-user I/O),
# so default to no access for "other" (dirs 0750) and an optional group for
# read. These are exported so the ebpf_trace.py child applies the SAME policy to
# the day dirs/files it creates (it reads EBPFM_DIR_MODE/EBPFM_FILE_MODE/
# EBPFM_GROUP). Empty EBPFM_GROUP = leave group as-is; a mode of empty or "0"
# means "leave whatever the umask produced" (opt out of tightening).
DIR_MODE="${EBPFM_DIR_MODE-0750}"
FILE_MODE="${EBPFM_FILE_MODE-0640}"
EBPFM_GROUP="${EBPFM_GROUP:-}"
export EBPFM_DIR_MODE="$DIR_MODE"
export EBPFM_FILE_MODE="$FILE_MODE"
export EBPFM_GROUP

# mkdir -p then apply the configured dir mode + optional group. Best-effort on
# chmod/chgrp: a failure must not stop collection. Used at every output mkdir.
_secure_dir() {
    local d="$1"
    mkdir -p "$d" || return 1
    case "$DIR_MODE" in ''|0) : ;; *) chmod "$DIR_MODE" "$d" 2>/dev/null || true ;; esac
    [ -n "$EBPFM_GROUP" ] && chgrp "$EBPFM_GROUP" "$d" 2>/dev/null || true
    return 0
}

# Apply the configured file mode + optional group to an existing file.
# Best-effort. The node tier's day-files are created by the shell and by
# `ebr.py append` (umask) and tightened here, before a record lands in them; the
# ebpf tier's files are handled in ebpf_trace.py.
_secure_file() {
    local f="$1"
    [ -e "$f" ] || return 0
    case "$FILE_MODE" in ''|0) : ;; *) chmod "$FILE_MODE" "$f" 2>/dev/null || true ;; esac
    [ -n "$EBPFM_GROUP" ] && chgrp "$EBPFM_GROUP" "$f" 2>/dev/null || true
    return 0
}

# The ebpf tier writes TWO streams per host per day; stream_of() in ebpf_trace.py
# is the authority on which record goes where. Each is a .jsonl, a .ebr, or both
# (EBPFM_FORMAT). The node tier is unaffected -- it emits snapshots only, into
# its own root, and keeps its single stream, <host>.jsonl and/or <host>.ebr.
#   <host>.exits.{jsonl,ebr}      exit, truncated   (per-process, unbounded rate)
#   <host>.snapshot.{jsonl,ebr}   everything else   (census, conns, submits, meta/stop)
EBPF_STREAMS="snapshot exits"
# The installed unit passes %H, the FULL hostname, where this script says
# `hostname -s`; a file under either name is this host's, so status and tail
# find the unit's files too. EXT is the format's extension, jsonl or ebr (with
# .gz for a sealed day).
_ebpf_file() {   # STREAM [DAY] [EXT=jsonl]
    local d="$OUTPUT_DIR/${2:-$(date +%F)}" x="${3:-jsonl}" g
    if [ ! -e "$d/$HOST.$1.$x" ]; then
        for g in "$d/$HOST".*."$1.$x"; do
            [ -e "$g" ] && { echo "$g"; return 0; }
        done
    fi
    echo "$d/$HOST.$1.$x"
}

# The file holding STEM's records in format EXT (jsonl, ebr) in DIR: the plain
# file, else its .gz (a sealed day), else nothing and status 1. Plain wins: while
# gzip runs both exist, and only the plain one is complete.
_twin() {   # DIR STEM EXT
    local f="$1/$2.$3"
    if [ -e "$f" ]; then echo "$f"; return 0; fi
    if [ -e "$f.gz" ]; then echo "$f.gz"; return 0; fi
    return 1
}

# Records in a day-file of either format, plain or gzipped: EBR's from its frame
# headers (nothing is decoded), JSONL's by counting lines. Empty if unreadable.
_feed_count() {   # FILE
    case "$1" in
        *.ebr|*.ebr.gz) python3 "$EBR" count "$1" 2>/dev/null ;;
        *.gz)           zcat -- "$1" 2>/dev/null | wc -l | tr -d ' ' ;;
        *)              wc -l < "$1" 2>/dev/null | tr -d ' ' ;;
    esac
}

# One line about a feed and its twin: the record count, then each format's
# size -- "1234 records, jsonl 21M | ebr 4.0M" -- or each format's own count when
# they differ (the live day's EBR can be a poll round behind its JSONL).
_twins_line() {   # NOUN FILE...   (files that do not exist are skipped)
    local noun="$1" f x n sz one="" all="" n0="" k=0 same=1
    shift
    for f in "$@"; do
        [ -f "$f" ] || continue
        x="${f%.gz}"; x="${x##*.}"
        n="$(_feed_count "$f")"; n="${n:-?}"
        sz="$(du -h "$f" | cut -f1)"
        k=$((k + 1))
        if [ "$k" = 1 ]; then n0="$n"; elif [ "$n" != "$n0" ]; then same=0; fi
        one="$one${one:+ | }$x $sz"
        all="$all${all:+ | }$x $n $noun $sz"
    done
    [ "$k" -gt 0 ] || return 1
    if [ "$same" = 1 ]; then echo "$n0 $noun, $one"; else echo "$all"; fi
}

fails=0
ok()   { printf '  ok    %-30s %s\n' "$1" "${2-}"; }
warn() { printf '  WARN  %-30s %s\n' "$1" "${2-}"; }
bad()  { printf '  FAIL  %-30s %s\n' "$1" "${2-}"; fails=$((fails+1)); }
die()  { printf 'ebpfm: %s\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" = 0 ] || die "must run as root (try: sudo $SELF $*)"; }

# --- tiers -----------------------------------------------------------------
# Two tiers, because only one of them needs root.
#
#   node   UNPRIVILEGED. Node health from /proc and the standard tools: load,
#          memory, filesystem space, NFS per-mount RTT, interface counters, the
#          D-state process census, the process tree, per-user cgroup totals, and
#          proc_stat.btime -- the reboot-era source for every dataset in this
#          study. eBPF cannot produce any of these: they are node aggregates and
#          kernel counters, not task events.
#   ebpf   ROOT. Exhaustive per-process events: every exit with its status, argv,
#          cwd, per-process I/O for ALL users, D-state dwell, per-process network
#          bytes, TCP connections. There is no unprivileged substitute -- netlink
#          proc connector, taskstats and inotify-on-/proc were all measured and
#          all fail (see collect/agents/poll_rate.md).
#
# root -> both tiers. Non-root -> the node tier alone, and `check` says exactly
# what is being skipped. EBPFM_TIER=node|ebpf|all overrides the default.
if [ -n "${EBPFM_TIER:-}" ]; then
    TIER="$EBPFM_TIER"
elif [ "$IS_ROOT" = 1 ]; then
    TIER=all
else
    TIER=node
fi
case "$TIER" in
    node|ebpf|all) ;;
    *) die "EBPFM_TIER must be node, ebpf or all (got '$TIER')" ;;
esac
want_node() { [ "$TIER" = node ] || [ "$TIER" = all ]; }
want_ebpf() { [ "$TIER" = ebpf ] || [ "$TIER" = all ]; }

# --- formats ---------------------------------------------------------------
# What both tiers write a day-file as: JSONL, what every reader has always read;
# EBR, lib/ebr.py's framed binary records (the same records, 4-8x smaller); or
# both, as twins holding the same records in the same order -- the default while
# EBR is on trial. Exported, so ebpf_trace.py writes what the node tier does
# (EBPFM_EBR_ZLIB and EBPFM_EBR_RESET_BYTES reach it the same way).
FORMAT="${EBPFM_FORMAT:-both}"
case "$FORMAT" in
    jsonl|ebr|both) ;;
    *) die "EBPFM_FORMAT must be jsonl, ebr or both (got '$FORMAT')" ;;
esac
export EBPFM_FORMAT="$FORMAT"
_want_jsonl() { [ "$FORMAT" != ebr ]; }
_want_ebr() { [ "$FORMAT" != jsonl ]; }
# the extension(s) of a day-file in this mode, for printed paths
_exts() { if [ "$FORMAT" = both ]; then printf '{jsonl,ebr}'; else printf '%s' "$FORMAT"; fi; }

# The node tier's collection mode follows the tier set: when the eBPF tier is
# also running it collects the four ptrace-crippled per-process sections
# properly, so the node tier stops duplicating them. On a host with no eBPF tier
# the node tier stays on `full` -- dropping them there would lose them outright.
# An explicit SNAPSHOT_MODE always wins.
_node_mode() {
    if   [ -n "${SNAPSHOT_MODE:-}" ]; then printf '%s' "$SNAPSHOT_MODE"
    elif want_ebpf;                   then printf 'node'
    else                                   printf 'full'
    fi
}

# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------
cmd_bootstrap() {
    need_root bootstrap
    echo "== bootstrap on $HOST ($(uname -r))"

    if command -v apt-get >/dev/null 2>&1; then
        _bootstrap_apt
    elif command -v dnf >/dev/null 2>&1; then
        _bootstrap_dnf
    elif command -v yum >/dev/null 2>&1; then
        _bootstrap_dnf yum
    else
        die "no apt-get/dnf/yum; install python3-bcc and kernel headers by hand"
    fi

    _set_sysctls
    echo "== re-checking"
    cmd_check
}

_bootstrap_apt() {
    # BCC lives in `universe` on Ubuntu. A fresh CloudLab image can have it
    # disabled, which shows up as an empty apt-cache Candidate rather than an
    # error. Add the component to the EXISTING stanzas so we keep the machine's
    # own mirror and signing key, instead of add-apt-repository, which would
    # itself need software-properties-common installed first.
    local changed=0 s
    for s in /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/*.sources; do
        [ -f "$s" ] || continue
        if grep -qE '^Components:' "$s" && ! grep -qE '^Components:.*universe' "$s"; then
            cp -n "$s" "$s.ebpfm.bak" 2>/dev/null || true
            sed -i -E 's/^(Components:.*)$/\1 universe/' "$s"
            echo "  + universe -> $s"
            changed=1
        fi
    done
    if [ -f /etc/apt/sources.list ] && grep -qE '^deb ' /etc/apt/sources.list \
            && ! grep -qE '^deb .*universe' /etc/apt/sources.list; then
        cp -n /etc/apt/sources.list /etc/apt/sources.list.ebpfm.bak 2>/dev/null || true
        sed -i -E 's/^(deb .*main.*)$/\1 universe/' /etc/apt/sources.list
        echo "  + universe -> /etc/apt/sources.list"
        changed=1
    fi

    echo "  apt-get update"
    DEBIAN_FRONTEND=noninteractive apt-get update -qq || warn apt-update "non-zero; continuing"

    local pkgs=()
    python3 -c 'import bcc' 2>/dev/null || pkgs+=(python3-bpfcc)
    [ -d "/lib/modules/$(uname -r)/build" ] || pkgs+=("linux-headers-$(uname -r)")
    command -v bpftool >/dev/null 2>&1 || pkgs+=("linux-tools-$(uname -r)" linux-tools-common)
    command -v ss >/dev/null 2>&1 || pkgs+=(iproute2)
    command -v jq >/dev/null 2>&1 || pkgs+=(jq)
    if [ "${#pkgs[@]}" -eq 0 ]; then
        echo "  all dependencies already present"
    else
        echo "  installing: ${pkgs[*]}"
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${pkgs[@]}" \
            || die "apt-get install failed (universe enabled? changed=$changed)"
    fi
}

_bootstrap_dnf() {
    local mgr="${1:-dnf}"
    local pkgs=()
    python3 -c 'import bcc' 2>/dev/null || pkgs+=(python3-bcc)
    [ -d "/lib/modules/$(uname -r)/build" ] || pkgs+=("kernel-devel-$(uname -r)")
    command -v bpftool >/dev/null 2>&1 || pkgs+=(bpftool)
    command -v ss >/dev/null 2>&1 || pkgs+=(iproute)
    command -v jq >/dev/null 2>&1 || pkgs+=(jq)
    if [ "${#pkgs[@]}" -eq 0 ]; then
        echo "  all dependencies already present"
    else
        echo "  installing: ${pkgs[*]}"
        "$mgr" install -y "${pkgs[@]}" || die "$mgr install failed"
    fi
}

_set_sysctls() {
    # Without these two, item 2 (uninterruptible-sleep stall time) and the
    # blkio/swapin dwell fields are silently zero rather than absent, which is the
    # worst possible failure for a measurement tool.
    local conf=/etc/sysctl.d/99-ebpfm.conf
    echo "== sysctls (required: two data points are inert without them)"
    local k v
    for k in kernel.sched_schedstats kernel.task_delayacct; do
        v="$(sysctl -n "$k" 2>/dev/null || echo missing)"
        if [ "$v" = missing ]; then
            warn "$k" "no such knob on this kernel (compile-time only)"
        elif [ "$v" = 1 ]; then
            ok "$k" "already 1"
        else
            sysctl -w "$k"=1 >/dev/null 2>&1 && ok "$k" "0 -> 1" || bad "$k" "could not set"
        fi
    done
    { echo "# written by ebpfm.sh bootstrap"
      echo "kernel.sched_schedstats = 1"
      echo "kernel.task_delayacct = 1"; } > "$conf" 2>/dev/null \
        && ok persisted "$conf" || warn persisted "could not write $conf"
}

# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------
cmd_check() {
    fails=0
    local K CFG
    K="$(uname -r)"; CFG="/boot/config-$K"
    [ -r "$CFG" ] || { [ -r /proc/config.gz ] && CFG=/proc/config.gz; }
    _cfg() {
        if [ "$CFG" = /proc/config.gz ]; then zcat "$CFG" 2>/dev/null | grep -E "^$1=" | cut -d= -f2
        elif [ -r "$CFG" ]; then grep -E "^$1=" "$CFG" 2>/dev/null | cut -d= -f2; fi
    }
    echo "== ebpfm check on $HOST (kernel $K)  [tier: $TIER]"

    printf -- '-- privilege\n'
    if [ "$IS_ROOT" = 1 ]; then
        ok euid "0 (root) -- both tiers available"
    elif want_ebpf; then
        bad euid "$(id -u): the ebpf tier needs root (re-run under sudo, or EBPFM_TIER=node)"
    else
        ok euid "$(id -u) (unprivileged) -- node tier only"
        printf '        skipping the ebpf tier: per-process exits, exit codes, argv, cwd,\n'
        printf '        all-user per-process I/O, D-state dwell, net bytes, TCP records.\n'
        printf '        There is no unprivileged substitute; re-run under sudo for those.\n'
    fi
    local ld; ld="$(cat /sys/kernel/security/lockdown 2>/dev/null | grep -o '\[[a-z]*\]')"
    case "$ld" in
        "[confidentiality]") bad lockdown "$ld blocks bpf_probe_read" ;;
        "") ok lockdown "not enforced" ;;
        *) ok lockdown "$ld" ;;
    esac

    _check_format
    if want_node; then _check_node_tier; fi
    if ! want_ebpf; then
        printf -- '-- vendored libraries\n'
        _check_vendored
        printf '== %d FAIL\n' "$fails"
        return "$fails"
    fi

    printf -- '-- cgroup placement (the drain is charged here)\n'
    local cg cgmax
    cg="$(awk -F: '$1==0{print $3}' /proc/self/cgroup 2>/dev/null)"
    cgmax="$(cat "/sys/fs/cgroup${cg}/cpu.max" 2>/dev/null || echo 'n/a')"
    ok cgroup "${cg:-unknown}"
    case "$cgmax" in
        "max "*) ok cpu.max "$cgmax (uncapped)" ;;
        n/a)     warn cpu.max "not readable" ;;
        *)       warn cpu.max "$cgmax — capped; 'start' uses systemd-run --slice=system.slice to escape it" ;;
    esac
    command -v systemd-run >/dev/null 2>&1 && ok systemd-run "$(command -v systemd-run)" \
        || warn systemd-run "absent; 'start' falls back to nohup inside this cgroup"

    printf -- '-- toolchain\n'
    if python3 -c 'import bcc' 2>/dev/null; then
        ok python3-bcc "$(python3 -c 'import bcc;print(getattr(bcc,"__version__","?"))' 2>/dev/null)"
    else
        bad python3-bcc "not importable — run: sudo $SELF bootstrap"
    fi
    [ -d "/lib/modules/$K/build" ] && ok "kernel headers" "/lib/modules/$K/build" \
        || warn "kernel headers" "missing (BCC needs them unless it can use BTF)"
    [ -r /sys/kernel/btf/vmlinux ] && ok BTF "$(stat -c %s /sys/kernel/btf/vmlinux) bytes" \
        || warn BTF "absent — tcp_btf and tcp_accept will be dropped"
    command -v bpftool >/dev/null 2>&1 && ok bpftool "$(command -v bpftool)" \
        || warn bpftool "absent — no BTF offsets, no overhead measurement"
    command -v ss >/dev/null 2>&1 && ok ss "$(command -v ss)" || warn ss "absent (netlink is used directly anyway)"

    printf -- '-- kernel config\n'
    local c v
    for c in CONFIG_BPF_SYSCALL CONFIG_BPF_EVENTS CONFIG_FTRACE_SYSCALLS; do
        v="$(_cfg $c)"; [ "$v" = y ] && ok "$c" y || bad "$c" "${v:-?}"
    done
    for c in CONFIG_TASK_IO_ACCOUNTING CONFIG_TASK_XACCT CONFIG_SCHED_INFO CONFIG_SCHEDSTATS \
             CONFIG_TASK_DELAY_ACCT CONFIG_DEBUG_INFO_BTF; do
        v="$(_cfg $c)"; [ "$v" = y ] && ok "$c" y || warn "$c" "${v:-?} (block dropped or degraded)"
    done

    printf -- '-- sysctls\n'
    for c in kernel.sched_schedstats kernel.task_delayacct; do
        v="$(sysctl -n "$c" 2>/dev/null || echo missing)"
        case "$v" in
            1) ok "$c" 1 ;;
            missing) warn "$c" "no such knob (compile-time only)" ;;
            *) bad "$c" "$v — dstate/blkio fields will read 0; run: sudo $SELF bootstrap" ;;
        esac
    done

    printf -- '-- tracepoints\n'
    local T=/sys/kernel/tracing/events
    [ -d "$T" ] || T=/sys/kernel/debug/tracing/events
    local tp
    for tp in sched/sched_process_fork sched/sched_process_exec sched/sched_process_exit; do
        [ -d "$T/$tp" ] && ok "$tp" present || bad "$tp" "MISSING (core)"
    done
    for tp in sched/sched_stat_blocked syscalls/sys_enter_write sock/inet_sock_set_state; do
        [ -d "$T/$tp" ] && ok "$tp" present || warn "$tp" "missing -> that block is dropped"
    done

    printf -- '-- kprobe symbols\n'
    local s
    for s in tcp_sendmsg tcp_recvmsg udp_sendmsg udp_recvmsg udpv6_sendmsg udpv6_recvmsg inet_csk_accept; do
        if grep -qwE " $s\$| $s " /proc/kallsyms 2>/dev/null || grep -qw "$s" /proc/kallsyms 2>/dev/null; then
            ok "$s" "in kallsyms"
        else
            case "$s" in
                udpv6_*)         warn "$s" "absent -> netbytes_udp6 dropped (IPv6 UDP uncounted)" ;;
                inet_csk_accept) warn "$s" "absent -> tcp_accept dropped" ;;
                *)               warn "$s" "absent -> netbytes and netpeer dropped" ;;
            esac
        fi
    done

    printf -- '-- netlink socket-diag\n'
    if python3 -c "import sys; sys.path.insert(0,'$DIR/lib'); import sockdiag; r=sockdiag.dump(); print(len(r))" \
            >/tmp/.ebpfm_nl 2>/tmp/.ebpfm_nl_err; then
        ok sock_diag "$(cat /tmp/.ebpfm_nl) sockets enumerated"
    else
        warn sock_diag "unreachable: $(head -1 /tmp/.ebpfm_nl_err 2>/dev/null)"
    fi
    rm -f /tmp/.ebpfm_nl /tmp/.ebpfm_nl_err

    printf -- '-- other-user /proc reads\n'
    local P
    P="$(ps -eo pid,uid --no-headers 2>/dev/null | awk '$2>=1000 && $2<60000 {print $1; exit}')"
    if [ -n "${P:-}" ]; then
        head -1 "/proc/$P/io" >/dev/null 2>&1 && ok "/proc/<other>/io" "readable (pid $P)" \
            || bad "/proc/<other>/io" "EACCES for pid $P"
        readlink "/proc/$P/cwd" >/dev/null 2>&1 && ok "/proc/<other>/cwd" "readlink ok" \
            || warn "/proc/<other>/cwd" "unreadable"
    else
        warn "/proc/<other>" "no non-root user process to test against"
    fi

    printf -- '-- vendored libraries\n'
    _check_vendored

    printf -- '-- python syntax\n'
    python3 -m py_compile "$TRACER" "$DIR"/lib/*.py 2>/dev/null && ok py_compile "all modules" \
        || bad py_compile "syntax error"

    printf '== %d FAIL\n' "$fails"
    return "$fails"
}

# Both tiers write in EBPFM_FORMAT, and EBR through lib/ebr.py: without a
# working one the eBPF tier falls back to JSONL alone and the node tier writes
# no EBR, so a format that asks for EBR makes that a FAIL.
_check_format() {
    printf -- '-- output format (both tiers)\n'
    case "$FORMAT" in
        both)  ok EBPFM_FORMAT "both: JSONL and EBR twins (the trial)" ;;
        jsonl) ok EBPFM_FORMAT "jsonl: no EBR twin" ;;
        ebr)   ok EBPFM_FORMAT "ebr: no JSONL -- every reader must read EBR" ;;
    esac
    local v
    if v="$(python3 "$EBR" --version 2>&1)"; then
        ok ebr.py "$v ($EBR)"
        _want_ebr && ok "EBR settings" \
            "zlib ${EBPFM_EBR_ZLIB:-0}, a segment per ${EBPFM_EBR_RESET_BYTES:-2097152} bytes"
    elif _want_ebr; then
        bad ebr.py "does not run ($(printf '%s' "$v" | tail -n 1)) -- no EBR would be written"
    else
        warn ebr.py "does not run ($(printf '%s' "$v" | tail -n 1)); unused with EBPFM_FORMAT=jsonl"
    fi
    return 0
}

_check_node_tier() {
    printf -- '-- node tier (unprivileged)\n'
    [ -f "$NODE_SNAPSHOT" ] && ok node_snapshot "$NODE_SNAPSHOT" \
        || bad node_snapshot "missing ($NODE_SNAPSHOT) -- re-vendor from collect/common/snapshot.py"
    if [ -f "$NODE_SNAPSHOT" ]; then
        python3 -m py_compile "$NODE_SNAPSHOT" 2>/dev/null && ok "node_snapshot syntax" ok \
            || bad "node_snapshot syntax" "py_compile failed"
    fi
    ok "snapshot mode" "$(_node_mode)$(want_ebpf && echo '  (ebpf tier carries the 4 dropped sections)' || echo '  (no ebpf tier: keeping every section)')"

    # snapshot.py shells out to these. A missing one degrades one section to
    # empty rather than failing, so they are warnings -- except ps, without
    # which most of the per-process sections are empty.
    local t
    for t in ps; do
        command -v "$t" >/dev/null 2>&1 && ok "$t" "$(command -v $t)" || bad "$t" "absent"
    done
    for t in mpstat iostat sar nfsiostat free ss uptime w; do
        command -v "$t" >/dev/null 2>&1 && ok "$t" "$(command -v $t)" \
            || warn "$t" "absent -> that section will be empty (sysstat / nfs-common / procps)"
    done

    [ -r /proc/stat ] && ok /proc/stat "btime=$(awk '/^btime/{print $2}' /proc/stat 2>/dev/null)" \
        || bad /proc/stat "unreadable -- btime is the reboot-era key for EVERY dataset"
    [ -d /sys/fs/cgroup/user.slice ] && ok cgroup-v2 "user.slice present" \
        || warn cgroup-v2 "no user.slice -> per_user_cgroup will be empty"

    if mkdir -p "$NODE_OUTPUT_DIR" 2>/dev/null && [ -w "$NODE_OUTPUT_DIR" ]; then
        ok "node output" "$NODE_OUTPUT_DIR (writable)"
    else
        bad "node output" "$NODE_OUTPUT_DIR not writable -- set EBPFM_NODE_OUTPUT_DIR"
    fi
}

_check_vendored() {
    # The vendored copies carry local additions, so a plain diff always differs.
    # Instead we record the UPSTREAM sha at vendor time and check whether upstream
    # has moved since, which is the thing that actually needs action.
    local man="$DIR/.vendored.sha256"
    [ -f "$man" ] || { warn vendored "no manifest ($man)"; return; }
    local repo="$DIR/.."
    local any=0 line sha path
    while read -r sha path; do
        [ -n "${sha:-}" ] || continue
        if [ -f "$repo/$path" ]; then
            any=1
            local now; now="$(sha256sum "$repo/$path" | cut -d' ' -f1)"
            if [ "$now" = "$sha" ]; then ok "$(basename "$path")" "upstream unchanged"
            else warn "$(basename "$path")" "UPSTREAM MOVED — re-vendor ($path)"; fi
        fi
    done < "$man"
    [ "$any" = 1 ] || ok vendored "standalone (no repo alongside; nothing to compare)"
}

# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
cmd_features() { need_root features; exec python3 "$TRACER" --features; }
cmd_dump_c()   { need_root dump-c;   exec python3 "$TRACER" --dump-c; }

# One node-tier snapshot appended to today's file(s). Unprivileged.
# node_snapshot.py prints one JSON line. It is captured first, so a snapshot the
# timeout kills appends nothing rather than half a line; then the line is
# appended to $HOST.jsonl and/or handed to `ebr.py append` for the $HOST.ebr
# twin (one frame per snapshot, continuing the file's segment, under its lock).
# node_snapshot.py itself is vendored and stays as it is (VENDORED.md).
_node_tick() {
    local d out rc=0
    d="$NODE_OUTPUT_DIR/$(date +%F)"
    _secure_dir "$d" || return 1
    # Bounded: one snapshot stuck on a hung mount would otherwise stall every
    # later one. The next tick simply tries again.
    out="$(SNAPSHOT_MODE="$(_node_mode)" $(_node_timeout) python3 "$NODE_SNAPSHOT" \
        "$HOST" "$NODE_TOP_N")" || return 1
    [ -n "$out" ] || return 1
    # Each file is created and given the configured mode/group before the
    # record lands, so no snapshot ever sits in a file at the umask's mode.
    if _want_jsonl; then
        { : >> "$d/$HOST.jsonl"; } 2>/dev/null; _secure_file "$d/$HOST.jsonl"
        printf '%s\n' "$out" >> "$d/$HOST.jsonl" || rc=1
    fi
    if _want_ebr; then
        { : >> "$d/$HOST.ebr"; } 2>/dev/null; _secure_file "$d/$HOST.ebr"
        if ! printf '%s\n' "$out" | python3 "$EBR" append --host "$HOST" --stream node \
                --writer node_snapshot.py "$d/$HOST.ebr"; then
            if _want_jsonl; then
                echo "ebpfm: node tier: EBR append to $d/$HOST.ebr failed (JSONL unaffected)" >&2
            else
                rc=1
            fi
        fi
    fi
    return "$rc"
}

# `timeout` with a limit just under the interval, when coreutils has it
_node_timeout() {
    command -v timeout >/dev/null 2>&1 || return 0
    local lim=$((NODE_INTERVAL - 10))
    [ "$lim" -lt 30 ] && lim=30
    printf 'timeout -k 10 %s' "$lim"
}

_node_loop() { while true; do _node_tick || true; sleep "$NODE_INTERVAL"; done; }

cmd_once() {
    local dur="${1:-${EBPFM_DURATION:-30}}"
    if want_node; then
        echo "node tier: one snapshot (mode=$(_node_mode)) -> $NODE_OUTPUT_DIR/$(date +%F)/$HOST.$(_exts)"
        _node_tick && echo "  ok" || echo "  FAILED"
    fi
    if want_ebpf; then
        need_root once
        _secure_dir "$OUTPUT_DIR"
        echo "ebpf tier: capturing ${dur}s on $HOST -> $OUTPUT_DIR/$(date +%F)/$HOST.{snapshot,exits}.$(_exts)"
        EBPFM_DURATION="$dur" python3 "$TRACER" "$HOST"
    fi
}

# A pid file outlives its process -- a crash, a reboot -- and the pid in it is
# then whatever the kernel handed it to next. Trust it only while that pid is
# still running what we started, or `stop` would signal a stranger.
_pid_runs() {   # _pid_runs PID WORD
    local cmd
    cmd="$(tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null)" || return 1
    case "$cmd" in *"$2"*) return 0 ;; esac
    return 1
}

_running_pid() {
    [ -f "$PID_FILE" ] || return 1
    local p; p="$(cat "$PID_FILE" 2>/dev/null)"
    [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null && _pid_runs "$p" ebpf_trace.py \
        && { echo "$p"; return 0; }
    return 1
}

_node_running_pid() {
    [ -f "$NODE_PID_FILE" ] || return 1
    local p; p="$(cat "$NODE_PID_FILE" 2>/dev/null)"
    [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null && _pid_runs "$p" _node_daemon \
        && { echo "$p"; return 0; }
    return 1
}

# The node tier is a plain nohup poll loop guarded by a pid file -- the same
# contract as collect/common/daemon.sh, so a supervisor may call `start` every
# cycle: an already-running daemon is left strictly alone. It needs no systemd
# and no cgroup escape; one snapshot is a few hundred ms of /proc reads.
_node_start() {
    local p
    if p="$(_node_running_pid)"; then
        echo "node tier already running on $HOST (pid $p)"
        return 1
    fi
    rm -f "$NODE_PID_FILE"
    if [ ! -f "$NODE_SNAPSHOT" ]; then
        echo "node tier NOT started: $NODE_SNAPSHOT is missing" >&2
        return 1
    fi
    _secure_dir "$NODE_OUTPUT_DIR" || { echo "node tier: cannot create dirs" >&2; return 1; }
    mkdir -p "$PID_DIR" || { echo "node tier: cannot create dirs" >&2; return 1; }
    SNAPSHOT_MODE="$(_node_mode)" nohup "$SELF" _node_daemon >>"$NODE_LOG" 2>&1 &
    local np=$!
    # Confirm the child SURVIVED before claiming success. $! is the nohup
    # process, which exits immediately if it could not exec -- so without this
    # `start` cheerfully reported a pid that was already gone, and only `status`
    # (if anyone looked) revealed the node tier had never run.
    sleep 0.3
    if ! kill -0 "$np" 2>/dev/null; then
        echo "node tier FAILED to start on $HOST -- see $NODE_LOG" >&2
        tail -n 2 "$NODE_LOG" 2>/dev/null | sed "s/^/    /" >&2
        rm -f "$NODE_PID_FILE"
        return 1
    fi
    echo "$np" > "$NODE_PID_FILE"
    echo "node tier started on $HOST (pid $np, mode=$(_node_mode), every ${NODE_INTERVAL}s)"
    echo "  output: $NODE_OUTPUT_DIR/<date>/$HOST.$(_exts)"
    return 0
}

_node_stop() {
    local p
    if p="$(_node_running_pid)"; then
        kill "$p" 2>/dev/null && echo "node tier stopped on $HOST (pid $p)"
    else
        echo "node tier not running on $HOST"
    fi
    rm -f "$NODE_PID_FILE"
}

_node_status() {
    local p b line
    if p="$(_node_running_pid)"; then
        echo "$NAME node tier: RUNNING on $HOST (pid $p, mode=$(_node_mode))"
    else
        echo "$NAME node tier: NOT RUNNING on $HOST"
    fi
    b="$NODE_OUTPUT_DIR/$(date +%F)/$HOST"
    if line="$(_twins_line snapshots "$b.jsonl" "$b.ebr")"; then
        echo "  today: $line"
    else
        echo "  today: no file yet ($b.$(_exts))"
    fi
}

_ebpf_start() {
    need_root start
    local p u
    # Two collectors on one node would double every record and fight over the
    # day file, so defer to an already-active unit instead of adding a second.
    if u="$(_active_unit)"; then
        echo "$NAME already running on $HOST (unit $u) -- not starting a second collector"
        return 1
    fi
    if p="$(_running_pid)"; then echo "$NAME already running on $HOST (pid $p)"; return 1; fi
    rm -f "$PID_FILE"
    _secure_dir "$OUTPUT_DIR"; mkdir -p "$PID_DIR"
    if command -v systemd-run >/dev/null 2>&1; then
        # into system.slice, so the drain is not charged to a capped user slice
        systemd-run --quiet --unit="${NAME}-${HOST}" --slice=system.slice \
            --property=Nice=19 --property=StandardOutput=append:"$LOG" \
            --property=StandardError=append:"$LOG" \
            --setenv=EBPFM_OUTPUT_DIR="$OUTPUT_DIR" \
            $(_passthru_env) \
            /usr/bin/env python3 "$TRACER" "$HOST" \
            || die "systemd-run failed"
        sleep 1
        local mp
        mp="$(systemctl show -p MainPID --value "${NAME}-${HOST}.service" 2>/dev/null)"
        [ -n "${mp:-}" ] && [ "$mp" != 0 ] && echo "$mp" > "$PID_FILE"
        echo "$NAME started on $HOST as unit ${NAME}-${HOST}.service (pid ${mp:-?})"
        echo "  cgroup: $(systemctl show -p ControlGroup --value "${NAME}-${HOST}.service" 2>/dev/null)"
    else
        warn systemd-run "absent — starting with nohup INSIDE the current cgroup"
        nohup nice -n 19 python3 "$TRACER" "$HOST" >>"$LOG" 2>&1 &
        echo $! > "$PID_FILE"
        echo "$NAME started on $HOST (pid $(cat "$PID_FILE"))"
    fi
    echo "  log:    $LOG"
    echo "  output: $OUTPUT_DIR/<date>/$HOST.{snapshot,exits}.$(_exts)"
}

_passthru_env() {
    local v
    for v in $(env | sed -n 's/^\(EBPFM_[A-Z_]*\)=.*/\1/p'); do
        [ "$v" = EBPFM_OUTPUT_DIR ] && continue
        printf -- '--setenv=%s=%s ' "$v" "${!v}"
    done
}

_ebpf_stop() {
    need_root stop
    local u
    if u="$(_active_unit)"; then
        systemctl stop "$u" 2>/dev/null \
            && echo "$NAME unit $u stopped on $HOST (SIGTERM -> truncated flush)"
        [ "$u" = "${NAME}.service" ] && \
            echo "  note: the persistent unit is still ENABLED; 'systemctl disable $NAME' to keep it down across reboots"
        rm -f "$PID_FILE"
        return 0
    fi
    local p
    if p="$(_running_pid)"; then
        kill -TERM "$p" && echo "$NAME stopped on $HOST (pid $p; SIGTERM -> truncated flush)"
    else
        echo "$NAME not running on $HOST"
    fi
    rm -f "$PID_FILE"
}

# The eBPF tier can be carried by either unit: the PERSISTENT one from
# install-unit (fleet deploys, survives reboot) or the TRANSIENT systemd-run
# scope from `start` (one-offs). Echo whichever is active, so status, start and
# stop all agree -- otherwise `status` cheerfully reports NOT RUNNING while the
# installed unit is collecting.
_active_unit() {
    local u
    for u in "${NAME}.service" "${NAME}-${HOST}.service"; do
        systemctl is-active --quiet "$u" 2>/dev/null && { echo "$u"; return 0; }
    done
    return 1
}

_ebpf_status() {
    local p u
    if u="$(_active_unit)"; then
        echo "$NAME: RUNNING on $HOST (unit $u, pid $(systemctl show -p MainPID --value "$u"))"
        echo "  cgroup: $(systemctl show -p ControlGroup --value "$u")"
        [ "$u" = "${NAME}.service" ] && echo "  persistent unit: $(systemctl is-enabled "$u" 2>/dev/null), Restart=$(systemctl show -p Restart --value "$u")"
    elif p="$(_running_pid)"; then
        echo "$NAME: RUNNING on $HOST (pid $p)"
    else
        echo "$NAME: NOT RUNNING on $HOST"
    fi
    # Both streams, separately: a snapshot file that is growing while the exits
    # file is flat is a real and diagnosable state (census ticking, no process
    # exits seen), and one summed number would hide it. Each with both formats'
    # sizes, whichever exist.
    local s line any=0
    for s in $EBPF_STREAMS; do
        if line="$(_twins_line records "$(_ebpf_file "$s")" "$(_ebpf_file "$s" "" ebr)")"; then
            any=1
            printf '  today (%-8s): %s\n' "$s" "$line"
        fi
    done
    [ "$any" = 1 ] || echo "  today: no files yet ($OUTPUT_DIR/$(date +%F)/$HOST.{snapshot,exits}.$(_exts))"
}

# --- persistent systemd unit ------------------------------------------------
# `start` uses a TRANSIENT systemd-run scope: correct for a one-off, but it does
# not survive a reboot and has no restart policy. For a fleet deployment the
# admin installs this instead, and systemd -- not run.sh, which is unprivileged
# and cannot sudo -- becomes the keepalive.
UNIT_SRC="$DIR/ebpfm.service"
UNIT_DST="/etc/systemd/system/${NAME}.service"

cmd_install_unit() {
    need_root install-unit
    [ -f "$UNIT_SRC" ] || die "missing unit template: $UNIT_SRC"
    command -v systemctl >/dev/null 2>&1 || die "no systemctl on this host"
    if _running_pid >/dev/null 2>&1; then
        die "a transient ebpfm is running (pid $(_running_pid)); '$SELF stop' first"
    fi
    mkdir -p "$OUTPUT_DIR"
    sed -e "s#@EBPFM_DIR@#$DIR#g" -e "s#@EBPFM_OUTPUT_DIR@#$OUTPUT_DIR#g" \
        "$UNIT_SRC" > "$UNIT_DST" || die "cannot write $UNIT_DST"
    chmod 0644 "$UNIT_DST"
    systemctl daemon-reload || die "daemon-reload failed"
    echo "installed $UNIT_DST"
    echo "  collector: $DIR/ebpf_trace.py"
    echo "  output:    $OUTPUT_DIR/<date>/<host>.{snapshot,exits}.{jsonl,ebr}"
    echo "             (which of the two: EBPFM_FORMAT in /etc/default/ebpfm, default both)"
    echo
    echo "  systemctl enable --now $NAME     # start it and survive reboots"
    echo "  systemctl status $NAME"
    echo
    echo "  The node tier is separate and unprivileged -- run '$SELF start' (or let"
    echo "  the repo keepalive do it). It needs no unit and no root."
}

cmd_uninstall_unit() {
    need_root uninstall-unit
    [ -f "$UNIT_DST" ] || { echo "no unit installed at $UNIT_DST"; return 0; }
    systemctl disable --now "$NAME" 2>/dev/null
    rm -f "$UNIT_DST"
    systemctl daemon-reload 2>/dev/null
    echo "removed $UNIT_DST"
}

# --- tier dispatchers ------------------------------------------------------
cmd_start() {
    local rc=1
    if want_node; then _node_start && rc=0; fi
    if want_ebpf; then _ebpf_start && rc=0; fi
    return "$rc"
}

cmd_stop() {
    # Stop the eBPF tier FIRST: SIGTERM makes it flush every live tracked process
    # as event="truncated", and those records are worth more with the node tier
    # still sampling alongside them.
    if want_ebpf; then _ebpf_stop; fi
    if want_node; then _node_stop; fi
}

cmd_status() {
    if want_node; then _node_status; fi
    if want_ebpf; then _ebpf_status; fi
}

# Today's file to read for STREAM: the JSONL while it is written, else the EBR
# twin, else a JSONL a run in another format left. Status 1 if there is none.
_tail_file() {   # STREAM
    local j e
    j="$(_ebpf_file "$1")"; e="$(_ebpf_file "$1" "" ebr)"
    if _want_jsonl && [ -f "$j" ]; then echo "$j"
    elif [ -f "$e" ]; then echo "$e"
    elif [ -f "$j" ]; then echo "$j"
    else return 1
    fi
}

# `tail [n] [stream]` -- both streams by default, since which one carries the
# record you are looking for is exactly what you may not know yet. JSONL is
# tailed as it always was; EBR through `ebr.py cat --tail`, which prints the
# same JSONL lines, newest frames first read from the end.
cmd_tail() {
    local n="${1:-3}" want="${2:-}" s f any=0
    for s in ${want:-$EBPF_STREAMS}; do
        f="$(_tail_file "$s")" || continue
        any=1
        echo "== $s ($f)"
        case "$f" in
            *.ebr)
                if command -v jq >/dev/null 2>&1; then python3 "$EBR" cat --tail "$n" "$f" | jq .
                else python3 "$EBR" cat --tail "$n" "$f"; fi ;;
            *)
                if command -v jq >/dev/null 2>&1; then tail -n "$n" "$f" | jq .
                else tail -n "$n" "$f"; fi ;;
        esac
    done
    [ "$any" = 1 ] || { echo "no log yet: $OUTPUT_DIR/$(date +%F)/$HOST.{snapshot,exits}.$(_exts)"; return 1; }
}

# ---------------------------------------------------------------------------
# EBR: the binary twin, and what it takes to trust it before relying on it
# ---------------------------------------------------------------------------
# `ebr VERB ...` is lib/ebr.py's own command line: cat, convert, verify, compare,
# count, append, bench, frames (`ebpfm.sh ebr --help`). Exit 0 ok, 1 a data
# problem, 2 a usage or I/O error.
cmd_ebr() { exec python3 "$EBR" "$@"; }

# The stems of DIR's day-files, once each: node7.exits for node7.exits.jsonl,
# node7.exits.ebr.gz and the rest.
_stems() {   # DIR
    local f s
    for f in "$1"/*.jsonl "$1"/*.jsonl.gz "$1"/*.ebr "$1"/*.ebr.gz; do
        [ -e "$f" ] || continue
        s="${f##*/}"; s="${s%.gz}"; echo "${s%.*}"
    done | sort -u
}

# Every stem of DIR that has both formats, compared by `ebr.py compare`: record
# by record, type-exact, key order included. Counts the pairs in _pairs.
_compare_dir() {   # DIR LIVE
    local dir="$1" live="$2" stem a b out rc=0
    for stem in $(_stems "$dir"); do
        a="$(_twin "$dir" "$stem" jsonl)" || a=""
        b="$(_twin "$dir" "$stem" ebr)" || b=""
        if [ -z "$a" ] || [ -z "$b" ]; then
            printf '  %-34s %s only: nothing to compare\n' "$stem" "$([ -n "$a" ] && echo jsonl || echo ebr)"
            continue
        fi
        _pairs=$((_pairs + 1))
        out="$(python3 "$EBR" compare $live "$a" "$b" 2>&1)" || rc=1
        printf '  %-34s %s\n' "$stem" "$(printf '%s\n' "$out" | tail -n 1)"
        printf '%s\n' "$out" | sed '$d' | sed 's/^/      /'
    done
    return "$rc"
}

# `compare [day] [--live]` -- every JSONL/EBR twin pair of the day (today by
# default), both tiers, sealed (.gz) or not. A live day's files are still being
# written, JSONL first in each round, so for today one may be a round ahead of
# the other: --live, which today implies, allows that and nothing else.
cmd_compare() {
    local day="" live="" a dir seen="" rc=0
    for a in "$@"; do
        case "$a" in
            --live) live=--live ;;
            -*) die "compare: unknown option '$a' (compare [day] [--live])" ;;
            *) day="$a" ;;
        esac
    done
    [ -n "$day" ] || day="$(date +%F)"
    [ "$day" = "$(date +%F)" ] && live=--live
    echo "== compare $day: JSONL vs EBR, record by record${live:+ (live: one may be a round ahead)}"
    _pairs=0
    for dir in "$OUTPUT_DIR/$day" "$NODE_OUTPUT_DIR/$day"; do
        [ -d "$dir" ] && [ "$dir" != "$seen" ] || continue
        seen="$dir"
        echo "-- $dir"
        _compare_dir "$dir" "$live" || rc=1
    done
    if [ "$_pairs" = 0 ]; then
        echo "== nothing compared: no day-file of $day under $OUTPUT_DIR or $NODE_OUTPUT_DIR has both formats"
        return 1
    fi
    if [ "$rc" = 0 ]; then echo "== $_pairs twin pairs, all equal"
    else echo "== $_pairs twin pairs, NOT all equal"; fi
    return "$rc"
}

# `verify [day]` -- every frame and record of the day's EBR files, both tiers. A
# pending tail (a round still being written) is reported, not a problem; a BAD
# region, a lost record or a frame whose header disagrees with its records is.
cmd_verify() {
    local day="${1:-$(date +%F)}" dir f seen=""
    local files=()
    for dir in "$OUTPUT_DIR/$day" "$NODE_OUTPUT_DIR/$day"; do
        [ -d "$dir" ] && [ "$dir" != "$seen" ] || continue
        seen="$dir"
        for f in "$dir"/*.ebr "$dir"/*.ebr.gz; do
            [ -e "$f" ] || continue
            case "$f" in *.gz) [ -e "${f%.gz}" ] && continue ;; esac   # plain beats .gz
            files+=("$f")
        done
    done
    if [ "${#files[@]}" -eq 0 ]; then
        echo "no EBR file for $day under $OUTPUT_DIR or $NODE_OUTPUT_DIR"
        return 1
    fi
    python3 "$EBR" verify "${files[@]}"
}

# `bench [file] [options]` -- EBR's size and codec speed against JSONL on one
# day-file, in memory (nothing is written): today's exits by default, else the
# newest exits file. Run on the collector host it gives that host's Python's
# numbers, which on EL8 is the 3.6 figure the flip to EBR waits for.
cmd_bench() {
    local f=""
    case "${1:-}" in ''|-*) ;; *) f="$1"; shift ;; esac
    if [ -z "$f" ]; then
        f="$(_ebpf_file exits)"
        [ -s "$f" ] || f="$(_ebpf_file exits "" ebr)"
        [ -s "$f" ] || f="$(ls -t "$OUTPUT_DIR"/*/"$HOST".exits.* "$OUTPUT_DIR"/*/"$HOST".*.exits.* \
                            2>/dev/null | head -n 1)"
    fi
    { [ -n "$f" ] && [ -s "$f" ]; } \
        || die "bench: no exits file to measure under $OUTPUT_DIR; name one: $SELF bench FILE"
    python3 "$EBR" bench "$@" "$f"
}

# ---------------------------------------------------------------------------
# overhead — a ONE-OFF measurement, not a shipped feature
# ---------------------------------------------------------------------------
cmd_overhead() {
    need_root overhead
    local reps="${EBPFM_BENCH_REPS:-3}"
    local tmp; tmp="$(mktemp -d)"
    local prev_stats; prev_stats="$(sysctl -n kernel.bpf_stats_enabled 2>/dev/null || echo missing)"
    echo "== overhead on $HOST ($(uname -r)), $reps timed reps per condition"
    echo "   this is a one-time validation; nothing here runs during normal collection"

    # Warm the feature cache FIRST. Without it the collector spends its first
    # ~40 s recompiling the program once per probed block, and a benchmark run
    # during that window measures a collector that has not attached anything yet
    # — which is how this first reported *negative* overhead.
    echo
    echo "-- warming the feature cache (first probe on a new kernel is slow)"
    local p0 p1
    p0="$(date +%s)"
    EBPFM_OUTPUT_DIR="$tmp" python3 "$TRACER" --features >/dev/null 2>&1 || true
    p1="$(date +%s)"
    printf '   probe took %ss (one-time per kernel; cached in %s)\n' "$((p1-p0))" "$tmp"

    # A first pass warms page cache and CPU frequency. Timing it would bias the
    # condition that happens to run first, which is always the baseline.
    echo
    echo "-- baseline (collector off, first pass discarded)"
    _bench_fork >/dev/null 2>&1; _bench_net >/dev/null 2>&1; _bench_disk >/dev/null 2>&1
    local b_fork b_net b_disk
    b_fork="$(_bench_median "$reps" _bench_fork)"
    b_net="$(_bench_median "$reps" _bench_net)"
    b_disk="$(_bench_median "$reps" _bench_disk)"
    printf '   fork/exec %8s ms   loopback %8s ms   disk %8s ms\n' "$b_fork" "$b_net" "$b_disk"

    echo
    echo "-- traced (collector on, benchmarks run under it)"
    # No fixed duration: the benchmark phase is what bounds the window, and a
    # duration that expires mid-benchmark would time an untraced tail. We SIGTERM
    # when the benchmarks finish, which also exercises the truncated flush.
    EBPFM_OUTPUT_DIR="$tmp" EBPFM_DURATION=3600 EBPFM_RESIDENCY_S=20 EBPFM_FLUSH=1 \
        python3 "$TRACER" "$HOST" >"$tmp/stderr" 2>&1 &
    local cpid=$!
    # wait for the probes to be attached, not a fixed guess
    local i live=0
    for i in $(seq 1 120); do
        grep -q 'running on' "$tmp/stderr" 2>/dev/null && { live=1; break; }
        kill -0 "$cpid" 2>/dev/null || break
        sleep 1
    done
    [ "$live" = 1 ] || { warn collector "never reported ready; see $tmp/stderr"; }
    # CPU baseline AFTER attach, so the probe cost is not charged to steady state
    local cpu0; cpu0="$(awk '{print $14+$15}' "/proc/$cpid/stat" 2>/dev/null || echo 0)"
    local t0; t0="$(date +%s)"
    _bench_fork >/dev/null 2>&1; _bench_net >/dev/null 2>&1; _bench_disk >/dev/null 2>&1
    local t_fork t_net t_disk
    t_fork="$(_bench_median "$reps" _bench_fork)"
    t_net="$(_bench_median "$reps" _bench_net)"
    t_disk="$(_bench_median "$reps" _bench_disk)"
    printf '   fork/exec %8s ms   loopback %8s ms   disk %8s ms\n' "$t_fork" "$t_net" "$t_disk"

    # Collector cost over exactly the benchmark window, read before the stats
    # window below can skew it. This is a DELTA from cpu0, so the one-time
    # compile/probe cost is excluded.
    local cpu1 ccpu crss window
    cpu1="$(awk '{print $14+$15}' "/proc/$cpid/stat" 2>/dev/null || echo 0)"
    window=$(( $(date +%s) - t0 ))
    [ "$window" -gt 0 ] || window=1
    ccpu="$(awk -v a="$cpu0" -v b="$cpu1" -v t="$(getconf CLK_TCK)" \
                'BEGIN{printf "%.2f", (b-a)/t}')"
    crss="$(awk '/VmRSS/{printf "%.0f", $2/1024}' "/proc/$cpid/status" 2>/dev/null || echo '?')"

    # Per-program attribution needs kernel.bpf_stats_enabled, which itself adds
    # two bpf_ktime_get_ns calls to every program run. Enabling it for the delta
    # above would inflate the delta, so it gets its own window: one more pass of
    # each workload, run only to accumulate counts.
    local progs=""
    if command -v bpftool >/dev/null 2>&1 && [ "$prev_stats" != missing ]; then
        sysctl -w kernel.bpf_stats_enabled=1 >/dev/null 2>&1
        _bench_fork >/dev/null 2>&1; _bench_net >/dev/null 2>&1; _bench_disk >/dev/null 2>&1
        progs="$(bpftool prog show -j 2>/dev/null \
            | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
rows=[]
for p in d:
    n=p.get("name") or ""
    if p.get("run_cnt",0) and p.get("type") in ("tracepoint","kprobe","raw_tracepoint","perf_event"):
        cnt=p["run_cnt"]; ns=p.get("run_time_ns",0)
        rows.append((ns/cnt if cnt else 0, n or str(p.get("id")), cnt, ns))
rows.sort(reverse=True)
for avg,n,cnt,ns in rows[:12]:
    print("   %-26s %10d events  %8.0f ns/event" % (n, cnt, avg))
')"
        sysctl -w kernel.bpf_stats_enabled=0 >/dev/null 2>&1
    fi

    kill -TERM "$cpid" 2>/dev/null
    wait "$cpid" 2>/dev/null

    [ "$prev_stats" != missing ] && [ "$prev_stats" != 1 ] \
        && sysctl -w kernel.bpf_stats_enabled="$prev_stats" >/dev/null 2>&1

    # `recs` is the whole capture, so it sums both streams, from whichever format
    # the run wrote (JSONL when it wrote both). The perf counters come off the
    # `stop` record, which rides the SNAPSHOT stream: `ebr.py cat --kind stop`
    # finds it in either format -- the last line of the exits file would
    # silently be an exit record and report every counter as null.
    local d; d="$tmp/$(date +%F)"
    local f s n recs=0 stop lostline seenline seentot bytesline
    for s in $EBPF_STREAMS; do
        f="$(_twin "$d" "$HOST.$s" jsonl)" || f="$(_twin "$d" "$HOST.$s" ebr)" || continue
        n="$(_feed_count "$f")"
        case "$n" in ''|*[!0-9]*) n=0 ;; esac
        recs=$((recs + n))
    done
    f="$(_twin "$d" "$HOST.snapshot" jsonl)" || f="$(_twin "$d" "$HOST.snapshot" ebr)" || f=""
    stop="$( [ -n "$f" ] && python3 "$EBR" cat --tail 1 --kind stop "$f" 2>/dev/null )"
    lostline="$(printf '%s' "$stop" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(json.dumps(d.get("perf_lost")))' 2>/dev/null || echo '{}')"
    seenline="$(printf '%s' "$stop" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(json.dumps(d.get("perf_seen")))' 2>/dev/null || echo '{}')"
    seentot="$(printf '%s' "$stop" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(sum((d.get("perf_seen") or {}).values()))' 2>/dev/null || echo 0)"
    # Per format, from stop.output: bytes and write(2)s up to the stop record.
    bytesline="$(printf '%s' "$stop" | python3 -c '
import json,sys
o=json.load(sys.stdin).get("output") or {}
p=["%s %.2f MB in %d writes" % (k, o[k]["bytes"]/1e6, o[k]["writes"]) for k in ("jsonl","ebr") if o.get(k)]
if o.get("jsonl") and o.get("ebr") and o["ebr"]["bytes"]:
    p.append("EBR %.1fx smaller" % (float(o["jsonl"]["bytes"])/o["ebr"]["bytes"]))
print(" | ".join(p) or "n/a")' 2>/dev/null || echo 'n/a')"

    echo
    echo "-- per BPF program (own stats window; the knob adds ~2 ktime calls/run)"
    [ -n "$progs" ] && echo "$progs" || echo "   (unavailable: needs bpftool and the bpf_stats knob)"
    echo
    echo "-- workload delta (median of $reps; bpf_stats OFF, so the knob does not inflate it)"
    _pct "fork/exec" "$b_fork" "$t_fork"
    _pct "loopback TCP" "$b_net" "$t_net"
    _pct "disk write" "$b_disk" "$t_disk"
    echo
    echo "-- collector itself (over the benchmark window, excluding the one-time probe)"
    printf '   %-26s %s CPU-seconds over %ss  (%.1f%% of one core)\n' "userspace CPU" "$ccpu" \
        "$window" "$(awk -v c="$ccpu" -v w="$window" 'BEGIN{printf "%.1f", c*100.0/w}')"
    printf '   %-26s %s MB  (BCC retains its clang/LLVM arena after compiling)\n' "RSS" "$crss"
    printf '   %-26s %s  (only TRACKED processes are emitted; the kernel probes\n' "records written" "$recs"
    printf '   %-26s  still ran on every event counted above)\n' ""
    printf '   %-26s %s\n' "bytes written" "$bytesline"
    printf '   %-26s %s\n' "events decoded" "$seenline"
    printf '   %-26s %s  (any non-zero means the drain fell behind)\n' "perf_lost" "$lostline"
    if [ "${seentot:-0}" -gt 0 ]; then
        echo
        echo "-- scaled: the figure to carry to a real node"
        printf '   %-26s %s events in %ss = %.0f/s\n' "drain rate here" "$seentot" "$window" \
            "$(awk -v n="$seentot" -v w="$window" 'BEGIN{printf "%.0f", n/w}')"
        printf '   %-26s %.1f us/event of userspace CPU\n' "per decoded event" \
            "$(awk -v c="$ccpu" -v n="$seentot" 'BEGIN{printf "%.1f", c*1e6/n}')"
        echo "   The benchmark drives events far faster than a login node does, so read"
        echo "   the percentage above as a ceiling and the us/event as the rate to scale."
    fi
    echo
    echo "Record these numbers in README.md with the node and kernel. Re-measure"
    echo "only when a probe is added or removed."
    rm -rf "$tmp"
}

_now_ms() { date +%s%3N; }
_bench_median() {           # reps, fn -> median ms
    local reps="$1" fn="$2" i t out=""
    for i in $(seq 1 "$reps"); do
        local s e; s="$(_now_ms)"; "$fn" >/dev/null 2>&1; e="$(_now_ms)"
        out="$out$((e-s))
"
    done
    printf '%s' "$out" | sort -n | awk 'NF{a[NR]=$1} END{print a[int((NR+1)/2)]}'
}
_bench_fork() { local i; for i in $(seq 1 3000); do /bin/true; done; }
_bench_disk() { dd if=/dev/zero of=/tmp/.ebpfm_bench bs=1M count=512 conv=fdatasync 2>/dev/null; rm -f /tmp/.ebpfm_bench; }
_bench_net()  {
    python3 - <<'PY'
import socket, threading
N = 256 << 20
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('127.0.0.1', 0)); srv.listen(1)
port = srv.getsockname()[1]
def sink():
    c, _ = srv.accept()
    while c.recv(1 << 20):
        pass
    c.close()
t = threading.Thread(target=sink); t.start()
s = socket.create_connection(('127.0.0.1', port))
buf = b'x' * (1 << 20)
sent = 0
while sent < N:
    sent += s.send(buf)
s.close(); t.join(); srv.close()
PY
}
_pct() {
    local name="$1" b="$2" t="$3"
    if [ -z "$b" ] || [ -z "$t" ] || [ "$b" = 0 ]; then
        printf '   %-26s %s -> %s ms\n' "$name" "${b:-?}" "${t:-?}"
    else
        printf '   %-26s %6s -> %6s ms   %+.1f%%\n' "$name" "$b" "$t" \
            "$(awk -v b="$b" -v t="$t" 'BEGIN{printf "%.1f", (t-b)*100.0/b}')"
    fi
}

# ---------------------------------------------------------------------------
# bundle — one self-extracting file, for handing to an admin
# ---------------------------------------------------------------------------
cmd_bundle() {
    local out="${1:-ebpfm-bundle.sh}"
    local base; base="$(basename "$DIR")"
    {
        cat <<'HDR'
#!/usr/bin/env bash
# Self-extracting ebpfm. Extracts to a temp dir and runs ebpfm.sh with
# whatever arguments you pass:   sudo ./ebpfm-bundle.sh check
set -euo pipefail
D="$(mktemp -d /tmp/ebpfm.XXXXXX)"
# Resolve our own path: $0 is whatever the caller typed, and `bash bundle.sh`
# gives a bare relative name that fails once anything changes directory.
SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"
sed -e '1,/^__EBPFM_PAYLOAD__$/d' "$SELF" | base64 -d | tar xz -C "$D"
echo "extracted to $D" >&2
exec bash "$D"/*/ebpfm.sh "$@"
__EBPFM_PAYLOAD__
HDR
        tar czf - -C "$DIR/.." \
            "$base/ebpfm.sh" "$base/ebpf_trace.py" "$base/node_snapshot.py" \
            "$base/ebpfm.service" \
            "$base/lib" "$base/README.md" \
            "$base/VENDORED.md" "$base/.vendored.sha256" 2>/dev/null | base64
    } > "$out"
    chmod +x "$out"
    case "$out" in /*) echo "wrote $out ($(du -h "$out" | cut -f1)) — run: sudo $out check" ;;
                     *)  echo "wrote $out ($(du -h "$out" | cut -f1)) — run: sudo ./$out check" ;; esac
}

# ---------------------------------------------------------------------------
case "${1:-}" in
    bootstrap) shift; cmd_bootstrap "$@" ;;
    check)     shift; cmd_check "$@" ;;
    features)  shift; cmd_features "$@" ;;
    dump-c)    shift; cmd_dump_c "$@" ;;
    once)      shift; cmd_once "$@" ;;
    start)     shift; cmd_start "$@" ;;
    stop)      shift; cmd_stop "$@" ;;
    status)    shift; cmd_status "$@" ;;
    overhead)  shift; cmd_overhead "$@" ;;
    tail)      shift; cmd_tail "$@" ;;
    compare)   shift; cmd_compare "$@" ;;
    verify)    shift; cmd_verify "$@" ;;
    bench)     shift; cmd_bench "$@" ;;
    ebr)       shift; cmd_ebr "$@" ;;
    install-unit)   shift; cmd_install_unit "$@" ;;
    uninstall-unit) shift; cmd_uninstall_unit "$@" ;;
    _node_daemon) shift; _node_loop ;;   # internal: the nohup'd node-tier child
    bundle)    shift; cmd_bundle "$@" ;;
    -h|--help|help|"")
        sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
    *) die "unknown verb '${1}'. Try: $0 --help" ;;
esac
