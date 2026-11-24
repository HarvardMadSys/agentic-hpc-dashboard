#!/usr/bin/env bash
# ebpfm.sh -- the one command for the login-node collector: install it on a node,
# check the node, start and stop both tiers, and say whether data is still
# arriving. `ebpfm.sh help` lists the commands, `ebpfm.sh <command> --help`
# explains one; OPERATIONS.md is the operator's guide, README.md says what is
# collected and why.
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
# (node|ebpf|all) overrides the choice. Both tiers write their day-files as JSONL.
#
# Settings: the environment, then /etc/default/ebpfm (the file the systemd units
# read, parsed the way systemd parses it), then the defaults. lib/ebpfm_settings.py
# holds the table of every setting; a command that runs something refuses a value
# it cannot use, with exit status 2, rather than start a tier that spins or writes
# nothing.
#
# Why `start` uses systemd-run: the userspace drain is cgroup-charged, and `sudo`
# does NOT leave the caller's slice -- a sudo'd child stays in
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
NAME="ebpfm"
IS_ROOT=0; [ "$(id -u)" = 0 ] && IS_ROOT=1

# The name the help screen and messages use: the command as it was typed, or its
# bare name where PATH finds this very file under it (the RPM's /usr/bin/ebpfm).
# A bundle passes its own (EBPFM_PROG): the copy running here is a temporary one.
_prog() {
    local b
    if [ -n "${EBPFM_PROG:-}" ]; then printf '%s' "$EBPFM_PROG"; return; fi
    b="$(basename "$0")"
    if [ "$(command -v "$b" 2>/dev/null)" = "$0" ]; then printf '%s' "$b"; else printf '%s' "$0"; fi
}
PROG="$(_prog)"
# What a hint says to run (with sudo): the bare name where PATH finds it, else an
# absolute path, which works from any folder (a bundle's own, not its temporary copy).
case "$PROG" in */*) HINT="${EBPFM_SELF:-$SELF}" ;; *) HINT="$PROG" ;; esac

# --- settings: the environment, then the settings file, then the defaults -------
# The settings file is the one the units read (EnvironmentFile=), parsed as systemd
# parses it: KEY=VALUE per line; `#` and `;` start comments; whitespace around the
# key, the `=` and the value does not count; one pair of quotes around the value
# comes off; the last assignment of a key wins. Each EBPFM_*/SNAPSHOT_* key in it
# that the environment does not set is exported, as the units' processes get it.
_ENV_NAMES="$(compgen -e | grep -E '^(EBPFM|SNAPSHOT)_' | tr '\n' ' ')"
DEFAULT_FILE="${EBPFM_DEFAULT_FILE-/etc/default/ebpfm}"
declare -A _FILE_VALS=() _FILE_LINES=()
_FILE_STATE=none
_FROM_FILE=()
_SHADOWED=()

_load_settings_file() {   # FILE
    local f="$1" line n=0 k v
    if [ -z "$f" ]; then _FILE_STATE=off; return 0; fi
    [ -e "$f" ] || { _FILE_STATE=none; return 0; }
    [ -r "$f" ] || { _FILE_STATE=unreadable; return 0; }
    _FILE_STATE=read
    while IFS= read -r line || [ -n "$line" ]; do
        n=$((n + 1))
        [[ $line =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]] || continue
        k="${BASH_REMATCH[1]}"; v="${BASH_REMATCH[2]}"
        v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
        case "$v" in \"*\") v="${v#\"}"; v="${v%\"}" ;; \'*\') v="${v#\'}"; v="${v%\'}" ;; esac
        case "$k" in EBPFM_*|SNAPSHOT_*) _FILE_VALS[$k]="$v"; _FILE_LINES[$k]="$n" ;; esac
    done < "$f"
    for k in "${!_FILE_VALS[@]}"; do
        if [[ -v $k ]]; then
            _SHADOWED+=("$k=${_FILE_LINES[$k]}")
        else
            export "$k=${_FILE_VALS[$k]}"
            _FROM_FILE+=("$k=${_FILE_LINES[$k]}")
        fi
    done
}
_load_settings_file "$DEFAULT_FILE"
_file_label() {
    case "$_FILE_STATE" in
        off)        echo "no settings file (EBPFM_DEFAULT_FILE is empty)" ;;
        unreadable) echo "$DEFAULT_FILE (unreadable to this user)" ;;
        *)          echo "$DEFAULT_FILE" ;;
    esac
}
_set_by_user() {   # NAME: set in the environment or the settings file
    case " $_ENV_NAMES " in *" $1 "*) return 0 ;; esac
    [ "${_FILE_VALS[$1]+set}" = set ]
}

# One host name for both tiers and every command: the short one, as the tracer
# and the units use (EBPFM_HOST overrides it everywhere at once).
HOST="${EBPFM_HOST:-$(hostname -s)}"

# Defaults differ by privilege: an unprivileged run can write neither /var/log
# nor /run, and the node tier is meant to work unprivileged.
if [ "$IS_ROOT" = 1 ]; then
    # Prefer /scratch: the collector's JSONL is unbounded and these login nodes
    # have a small root disk, while /scratch is large and root-writable. Fall
    # back to /var/log/ebpfm where /scratch is absent (dev boxes, CI, other
    # sites). The units, `install` and a running tier's note (below) fix the
    # folder once chosen, so a /scratch that mounts late does not move it.
    if [ -d /scratch ] && [ -w /scratch ]; then
        _DEF_OUT=/scratch/ebpfm;      _DEF_NODE=/scratch/ebpfm-login;   _DEF_PID=/run
    else
        _DEF_OUT=/var/log/ebpfm;      _DEF_NODE=/var/log/ebpfm-login;   _DEF_PID=/run
    fi
else
    _DEF_OUT="$HOME/.ebpfm/ebpf";     _DEF_NODE="$HOME/.ebpfm/login";   _DEF_PID="$HOME/.run"
fi
_abs() { case "$1" in /*|'') printf '%s' "$1" ;; *) printf '%s/%s' "$PWD" "$1" ;; esac; }
OUTPUT_DIR="$(_abs "${EBPFM_OUTPUT_DIR:-$_DEF_OUT}")"
NODE_OUTPUT_DIR="$(_abs "${EBPFM_NODE_OUTPUT_DIR:-$_DEF_NODE}")"
PID_DIR="$(_abs "${EBPFM_PID_DIR:-$_DEF_PID}")"
PID_FILE="$PID_DIR/${NAME}_${HOST}.pid"
NODE_PID_FILE="$PID_DIR/${NAME}-node_${HOST}.pid"
# A running tier's note of its settings, beside its pid file (see _write_state)
STATE_FILE="$PID_DIR/${NAME}_${HOST}.env"
NODE_STATE_FILE="$PID_DIR/${NAME}-node_${HOST}.env"
LOG="$OUTPUT_DIR/${NAME}_${HOST}.log"
NODE_LOG="$NODE_OUTPUT_DIR/${NAME}-node_${HOST}.log"
NODE_INTERVAL="${EBPFM_NODE_INTERVAL:-300}"
NODE_TOP_N="${EBPFM_NODE_TOP_N:-25}"
EBPF_INTERVAL="${EBPFM_RESIDENCY_S:-60}"
export EBPFM_OUTPUT_DIR="$OUTPUT_DIR"
SYSCTL_CONF="${EBPFM_SYSCTL_CONF:-/etc/sysctl.d/99-ebpfm.conf}"

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
# Best-effort. The node tier's day-file is created by the shell (umask) and
# tightened here, before a record lands in it; the ebpf tier's files are handled
# in ebpf_trace.py.
_secure_file() {
    local f="$1"
    [ -e "$f" ] || return 0
    case "$FILE_MODE" in ''|0) : ;; *) chmod "$FILE_MODE" "$f" 2>/dev/null || true ;; esac
    [ -n "$EBPFM_GROUP" ] && chgrp "$EBPFM_GROUP" "$f" 2>/dev/null || true
    return 0
}

# The ebpf tier writes TWO files per host per day; stream_of() in ebpf_trace.py
# is the authority on which record goes where. The node tier is unaffected --
# it emits snapshots only, into its own root, and keeps its single file.
#   <host>.exits.jsonl      exit, truncated   (per-process, unbounded rate)
#   <host>.snapshot.jsonl   everything else   (census, conns, submits, meta/stop)
EBPF_STREAMS="snapshot exits"
# Before ebpfm.sh and the unit agreed on the short host name, the unit passed %H,
# the FULL hostname; a file under either name is this host's, so status and tail
# find the old unit's files too.
_ebpf_file() {   # STREAM [DAY]
    local d="$OUTPUT_DIR/${2:-$(date +%F)}" g
    if [ ! -e "$d/$HOST.$1.jsonl" ]; then
        for g in "$d/$HOST".*."$1".jsonl; do
            [ -e "$g" ] && { echo "$g"; return 0; }
        done
    fi
    echo "$d/$HOST.$1.jsonl"
}

# The day-file holding STEM's records in DIR: STEM.jsonl, else its .gz (a sealed
# day), else nothing and status 1. Plain wins: while gzip runs both exist, and
# only the plain one is complete.
_jsonl_file() {   # DIR STEM
    local f="$1/$2.jsonl"
    if [ -e "$f" ]; then echo "$f"; return 0; fi
    if [ -e "$f.gz" ]; then echo "$f.gz"; return 0; fi
    return 1
}

# Records in a day-file, plain or gzipped: one per line. Empty if unreadable.
_feed_count() {   # FILE
    case "$1" in
        *.gz) zcat -- "$1" 2>/dev/null | wc -l | tr -d ' ' ;;
        *)    wc -l < "$1" 2>/dev/null | tr -d ' ' ;;
    esac
}

# The last `stop` record in a day-file, plain or gzipped, as its line: a run's
# own summary, wherever in the file it landed. Empty if there is none.
_last_stop() {   # FILE
    python3 -c '
import gzip,json,sys
p=sys.argv[1]
last=""
kw=dict(encoding="utf-8",errors="replace")
with (gzip.open(p,"rt",**kw) if p.endswith(".gz") else open(p,**kw)) as fh:
    for line in fh:
        if "\"stop\"" not in line:
            continue
        try:
            if json.loads(line).get("event")=="stop":
                last=line
        except Exception:
            pass
sys.stdout.write(last)' "$1" 2>/dev/null
}

# --- check's lines, and what they add up to ----------------------------------------
fails=0
CK_TIER=both          # the tier the check lines below belong to, for the verdict
CK_FAILS_NODE=()      # name<US>message<US>fix, per FAIL that stops the node tier (or both)
CK_FAILS_EBPF=()      # ... the eBPF tier alone
CK_NO_VERDICT=0
_US=$'\x1f'
ok()   { printf '  ok    %-30s %s\n' "$1" "${2-}"; }
warn() { printf '  WARN  %-30s %s\n' "$1" "${2-}"; }
bad()  {   # NAME MESSAGE [FIX]
    printf '  FAIL  %-30s %s\n' "$1" "${2-}"
    [ -n "${3-}" ] && printf '        %-30s fix: %s\n' "" "$3"
    fails=$((fails+1))
    case "$CK_TIER" in
        ebpf) CK_FAILS_EBPF+=("$1$_US${2-}$_US${3-}") ;;
        *)    CK_FAILS_NODE+=("$1$_US${2-}$_US${3-}") ;;
    esac
    return 0
}
die()  { printf 'ebpfm: %s\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" = 0 ] || die "must run as root (try: sudo $HINT $*)"; }

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
# what is being skipped. EBPFM_TIER=node|ebpf|all overrides the default; any
# other value is refused by the settings check before anything runs.
case "${EBPFM_TIER:-}" in
    node|ebpf|all) TIER="$EBPFM_TIER" ;;
    *) if [ "$IS_ROOT" = 1 ]; then TIER=all; else TIER=node; fi ;;
esac
want_node() { [ "$TIER" = node ] || [ "$TIER" = all ]; }
want_ebpf() { [ "$TIER" = ebpf ] || [ "$TIER" = all ]; }

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

# What lib/ebpfm_settings.py needs to say where each value came from.
SETTINGS_ARGS=(--env-names "$_ENV_NAMES" --file "$DEFAULT_FILE" --file-state "$_FILE_STATE")
for _kv in "${_FROM_FILE[@]+"${_FROM_FILE[@]}"}"; do SETTINGS_ARGS+=(--origin "$_kv"); done
for _kv in "${_SHADOWED[@]+"${_SHADOWED[@]}"}"; do SETTINGS_ARGS+=(--shadowed "$_kv"); done
SETTINGS_ARGS+=(--default "EBPFM_TIER=$([ "$IS_ROOT" = 1 ] && echo all || echo node)"
                --default "EBPFM_OUTPUT_DIR=$_DEF_OUT" --default "EBPFM_NODE_OUTPUT_DIR=$_DEF_NODE"
                --default "EBPFM_PID_DIR=$_DEF_PID" --default "SNAPSHOT_MODE=$(want_ebpf && echo node || echo full)"
                --default "EBPFM_HOST=$(hostname -s)")
_settings_py() { python3 "$DIR/lib/ebpfm_settings.py" "$@"; }
_version() { python3 "$DIR/lib/ebpfm_version.py" "$DIR" 2>/dev/null || echo unknown; }

# Before a command that runs something: a setting it cannot use stops it here,
# with exit status 2, instead of a loop that spins on a bad interval or a tier
# that writes nothing. An EBPFM_*/SNAPSHOT_* name that is no setting is named,
# with the nearest one, and does not stop anything.
_require_valid_settings() {   # COMMAND
    local rc
    command -v python3 >/dev/null 2>&1 || die "python3 not found: both tiers need it"
    _settings_py validate "${SETTINGS_ARGS[@]}"; rc=$?
    case "$rc" in
        0) return 0 ;;
        2) printf 'ebpfm: %s: not run: fix the setting above (%s config shows every setting and where it is set)\n' \
               "$1" "$PROG" >&2
           exit 2 ;;
        *) printf 'ebpfm: warning: could not check the settings (lib/ebpfm_settings.py exited %s)\n' "$rc" >&2 ;;
    esac
}

# --- help ------------------------------------------------------------------------
COMMANDS="install check bootstrap install-unit uninstall-unit start stop status once tail config version features dump-c overhead bundle help"

cmd_help() {   # [COMMAND]
    if [ -n "${1:-}" ]; then _usage "$1"; return 0; fi
    cat <<EOF
Usage: $PROG <command> [options]        ($PROG <command> --help explains one)

The login-node collector, in two tiers: the node tier (any user: load, memory,
filesystems, NFS, per-user cgroups, a snapshot every ${NODE_INTERVAL}s) and the eBPF tier
(root: each process's exit, with its command line, working directory, I/O,
network and how it ended). As root you get both; as a user, the node tier.

Set up
  install           the whole first install on this node: dependencies, the
                    settings file, both systemd units started, a check    [root]
  check             test this node, one line per precondition (exit = FAILs)
  bootstrap         install the dependencies, set two kernel settings     [root]
  install-unit      write the two systemd units; start nothing           [root]
  uninstall-unit    stop and remove them                                 [root]
Run
  start | stop      both tiers in the background, until a reboot (a user: the node tier)
  status            is each tier running, and is data still arriving? (--json)
  once [SECS]       one capture in the foreground, SECS long (30)
Look
  tail [N] [STREAM] the last N records of today's files (STREAM: snapshot, exits, node)
  config [NAME...]  each setting: its value, its default and where it came from
  version           this collector's version (also: $PROG --version)
  features          the BPF blocks this kernel loads                     [root]
  dump-c            the BPF C it compiles, for a block that will not load [root]
  overhead          a one-off cost benchmark: minutes; writes 512 MB to /tmp [root]
  bundle [FILE]     write the collector as one self-extracting file
  help [COMMAND]    this list, or what COMMAND does (as: $PROG COMMAND --help)

Data      eBPF tier  $OUTPUT_DIR/<date>/$HOST.{snapshot,exits}.jsonl
          node tier  $NODE_OUTPUT_DIR/<date>/$HOST.jsonl
Settings  the environment, then $(_file_label), then defaults: $PROG config
Docs      $DIR/OPERATIONS.md (running it), $DIR/README.md (what it records)
Exit      0 ok · 1 failed (status: degraded or stale) · 2 bad usage or setting
          3 not running (status) · 4 unknown, or started but not yet confirmed
EOF
}

_usage() {   # COMMAND
    case "$1" in
        install) cat <<EOF
Usage: $PROG install [--dry-run] [--no-wait]                                [root]

The whole first install on this node, in order:
  1. the dependencies (python3-bcc, the kernel's headers, bpftool, iproute, jq)
     and the two kernel settings the eBPF tier needs, as \`bootstrap\` does;
  2. EBPFM_OUTPUT_DIR and EBPFM_NODE_OUTPUT_DIR written into $(_file_label)
     (made from the commented defaults if missing; every other line kept), so
     each later command and both units agree on where the day-files go;
  3. both systemd units (ebpfm.service, ebpfm-node.service), as \`install-unit\`;
  4. a check; each tier that passes it is enabled and started, and its first
     record waited for;
  5. a verdict: READY: both tiers, READY: node tier only, or NOT READY: N FAIL,
     each FAIL with its fix.
Run it again after an upgrade: what is set stays. It refuses a temporary copy a
bundle unpacked, and a node where the ebpfm-collector package is installed.

  --dry-run   print the plan and change nothing (needs no root)
  --no-wait   do not wait for the first records
Exit: 0 READY: both tiers; 1 anything less; 4 a tier started but is not yet confirmed.
EOF
        ;;
        check) cat <<EOF
Usage: $PROG check

One line per precondition of each tier this user runs (EBPFM_TIER): the
settings, the node tier's tools and output folder, then for the eBPF tier
privilege, BCC, kernel headers and config, the two sysctls, tracepoints, kprobe
symbols, netlink, the cgroup its drain is charged to, and its output folder.
Each FAIL says how to fix it; the last lines are a verdict: READY: both tiers,
READY: node tier only, or NOT READY: N FAIL. It changes nothing.
Exit: the number of FAILs.
EOF
        ;;
        bootstrap) cat <<EOF
Usage: $PROG bootstrap [--dry-run]                                          [root]

Installs whichever of these are missing, with apt-get, dnf or yum: python3-bcc
(python3-bpfcc on Ubuntu, adding the \`universe\` component to the apt sources
that lack it), the running kernel's headers, bpftool, iproute and jq. Sets
kernel.sched_schedstats=1 and kernel.task_delayacct=1, now and in
$SYSCTL_CONF. Deletes the feature cache, then runs check.
  --dry-run   print what it would install and set, and change nothing
Exit: the number of FAILs in the closing check.
EOF
        ;;
        install-unit) cat <<EOF
Usage: $PROG install-unit [--dry-run]                                       [root]

Writes ebpfm.service (the eBPF tier) and ebpfm-node.service (the node tier) into
EBPFM_UNIT_DIR ($UNIT_DIR), with this folder's paths and the output folders
filled in, and reloads systemd. It enables and starts nothing:
    systemctl enable --now ebpfm ebpfm-node
It refuses while a tier \`start\` began is running, and from a temporary copy a
bundle unpacked. \`install\` does this and the rest of a first install.
  --dry-run   print what it would write, and change nothing
EOF
        ;;
        uninstall-unit) cat <<EOF
Usage: $PROG uninstall-unit [--dry-run]                                     [root]

Stops and disables ebpfm.service and ebpfm-node.service, removes them from
EBPFM_UNIT_DIR ($UNIT_DIR) and reloads systemd. The day-files,
$DEFAULT_FILE and $SYSCTL_CONF stay (OPERATIONS.md, "Removing it").
  --dry-run   print what it would stop and remove, and change nothing
EOF
        ;;
        start) cat <<EOF
Usage: $PROG start [--no-wait | --wait SECS]

Starts each tier this user runs (EBPFM_TIER; as a user, the node tier) in the
background until the next reboot: the eBPF tier as a transient systemd service
in system.slice (with nohup where systemd is not running), then the node tier as
a loop -- keeping every section if the eBPF tier did not start. It then waits
for evidence that each tier collects (the eBPF tier's \`meta\` record, the node
tier's first snapshot) and prints the log's last lines if one dies. A tier that
already runs, as a unit or a loop, is left alone. \`install\` makes units that
survive a reboot.
  --no-wait     return once each tier is running, without waiting for records
  --wait SECS   wait up to SECS for each tier (60)
Exit: 0 running and confirmed, or already running; 1 a tier failed;
      4 started, not confirmed within the wait ($PROG status will say).
EOF
        ;;
        stop) cat <<EOF
Usage: $PROG stop

Stops each tier this user runs: the eBPF tier first, which writes every live
process as \`truncated\` as it stops, then the node tier, after the snapshot it
may be taking. A tier that runs as a unit is stopped through systemd and stays
enabled (systemctl disable ... keeps it down across reboots).
Exit: 0 stopped, or not running; 1 a tier could not be stopped (it says why).
EOF
        ;;
        status) cat <<EOF
Usage: $PROG status [--json]

For each tier this user runs: whether it runs (its unit or pid), how old its
newest record is against twice its interval (older is STALE), today's records
and size, its log, the version it runs against the version installed, the eBPF
tier's perf_lost, and the free space where it writes. A tier started from
another shell is found from its note in EBPFM_PID_DIR ($PID_DIR).
  --json   the same, as one JSON object
Exit: 0 healthy; 1 degraded or stale (or one tier of two not running);
      3 not running; 4 unknown (a tier runs, but its files cannot be read).
EOF
        ;;
        once) cat <<EOF
Usage: $PROG once [SECS]

One capture in the foreground: a node snapshot appended to today's file, then,
as root, the eBPF tier for SECS seconds (EBPFM_DURATION, else 30), into the same
day-files \`start\` writes.
EOF
        ;;
        tail) cat <<EOF
Usage: $PROG tail [N] [STREAM]

The last N records (3) of today's files, pretty-printed by jq where it is
installed. STREAM: snapshot or exits (the eBPF tier's two files), or node; by
default the eBPF tier's two, or the node tier's for a user who runs only that.
EOF
        ;;
        config) cat <<EOF
Usage: $PROG config [--json] [NAME...]
       $PROG config --defaults-file

Every setting both tiers and this command read: its value, where that came from
(the environment, the settings file and its line, or the default), and the
default where they differ; NAME narrows the list. A value a tier cannot use is
marked INVALID, and an EBPFM_*/SNAPSHOT_* name that is no setting is named, with
the nearest one. Precedence: the environment, then $(_file_label), then the
defaults. OPERATIONS.md has the table of every setting.
  --json            the same, as JSON
  --defaults-file   print the commented defaults file (every setting at its default)
Exit: 2 if a value is invalid, else 0.
EOF
        ;;
        version) cat <<EOF
Usage: $PROG version      (or: $PROG --version)

This collector's version: the VERSION file a build stamped (the RPM, a bundle),
else \`git describe --always --dirty\` in a checkout, else sha256: and a hash of
the code. The eBPF tier records it in its \`meta\` record as collector_version,
and \`status\` warns when a running tier's differs from this.
EOF
        ;;
        features) cat <<EOF
Usage: $PROG features                                                      [root]

Probes each BPF block on this kernel and prints, as JSON, those that load and
why each other one does not (EBPFM_PROBE_VERBOSE=1 adds the compiler's
message). The first probe on a kernel compiles for about 40 s; the result is
cached in EBPFM_OUTPUT_DIR.
EOF
        ;;
        dump-c) cat <<EOF
Usage: $PROG dump-c                                                        [root]

Prints the BPF C the tracer compiles for the blocks this kernel loads, to read
beside a block's rejection.
EOF
        ;;
        overhead) cat <<EOF
Usage: $PROG overhead                                                      [root]

A one-off cost measurement (README.md, "Measured overhead"): benchmarks a fork
storm, loopback TCP and a 512 MB write to /tmp with the collector off and on
(EBPFM_BENCH_REPS times each), then the collector's own CPU and memory. It takes
minutes, and nothing of it runs during normal collection.
EOF
        ;;
        bundle) cat <<EOF
Usage: $PROG bundle [FILE]

Writes the collector as one self-extracting shell file (FILE; ebpfm-bundle.sh),
stamped with this copy's version. Run it with any command: it unpacks itself to
a temporary folder, runs the command there and deletes the folder (\`start\`
keeps it: the tiers run from it):
    sudo ./ebpfm-bundle.sh check
To install from it, unpack it somewhere permanent first:
    sudo ./ebpfm-bundle.sh --extract /opt/ebpfm && sudo /opt/ebpfm/ebpfm.sh install
EOF
        ;;
        help) cat <<EOF
Usage: $PROG help [COMMAND]

The list of commands, or what COMMAND does.
EOF
        ;;
    esac
}

# --- the command line --------------------------------------------------------------
OPT_DRY_RUN=0; OPT_NO_WAIT=0; OPT_WAIT=60; OPT_JSON=0; OPT_DEFAULTS_FILE=0
ARGS=()

# COMMAND -> "the options it takes|how many arguments"
_spec() {
    case "$1" in
        help)                                          echo "|1" ;;
        version|check|stop|features|dump-c|overhead)   echo "|0" ;;
        install)                                       echo "--dry-run --no-wait|0" ;;
        bootstrap|install-unit|uninstall-unit)         echo "--dry-run|0" ;;
        start)                                         echo "--no-wait --wait|0" ;;
        status)                                        echo "--json|0" ;;
        once)                                          echo "|1" ;;
        tail)                                          echo "|2" ;;
        config)                                        echo "--json --defaults-file|64" ;;
        bundle)                                        echo "|1" ;;
        *) return 1 ;;
    esac
}

_usage_error() {   # COMMAND MESSAGE
    printf 'ebpfm: %s: %s (see: %s %s --help)\n' "$1" "$2" "$PROG" "$1" >&2
    exit 2
}

_unknown_command() {   # WORD
    local near
    near="$(python3 -c 'import difflib, sys
m = difflib.get_close_matches(sys.argv[1], sys.argv[2:], 1, 0.6)
print(m[0] if m else "")' "$1" $COMMANDS 2>/dev/null)"
    printf "ebpfm: unknown command '%s'%s (see: %s --help)\n" "$1" \
        "${near:+; did you mean '$near'?}" "$PROG" >&2
    exit 2
}

# -h/--help anywhere prints the command's usage and does nothing else; an option
# the command does not take, or an argument too many, is refused with exit 2.
_parse_args() {   # COMMAND ARGS...
    local cmd="$1" spec opts max a
    shift
    spec="$(_spec "$cmd")"; opts=" ${spec%%|*} "; max="${spec##*|}"
    for a in "$@"; do
        case "$a" in --) break ;; -h|--help) _usage "$cmd"; exit 0 ;; esac
    done
    ARGS=()
    while [ $# -gt 0 ]; do
        a="$1"; shift
        case "$a" in
            --) ARGS+=("$@"); break ;;
            --wait|--wait=*)
                case "$opts" in *" --wait "*) ;; *) _usage_error "$cmd" "unknown option '$a'" ;; esac
                if [ "$a" = --wait ]; then
                    [ $# -gt 0 ] || _usage_error "$cmd" "--wait needs a number of seconds"
                    OPT_WAIT="$1"; shift
                else
                    OPT_WAIT="${a#--wait=}"
                fi
                case "$OPT_WAIT" in ''|*[!0-9]*) _usage_error "$cmd" "--wait '$OPT_WAIT': expected whole seconds" ;; esac
                ;;
            -?*)
                case "$opts" in *" $a "*) ;; *) _usage_error "$cmd" "unknown option '$a'" ;; esac
                case "$a" in
                    --dry-run)       OPT_DRY_RUN=1 ;;
                    --no-wait)       OPT_NO_WAIT=1 ;;
                    --json)          OPT_JSON=1 ;;
                    --defaults-file) OPT_DEFAULTS_FILE=1 ;;
                esac
                ;;
            *) ARGS+=("$a") ;;
        esac
    done
    [ "${#ARGS[@]}" -le "$max" ] || _usage_error "$cmd" "unexpected argument '${ARGS[$max]}'"
    case "$cmd" in
        once)
            if [ "${#ARGS[@]}" -gt 0 ]; then
                case "${ARGS[0]}" in ''|*[!0-9]*|0) _usage_error once "SECS '${ARGS[0]}': expected whole seconds (>= 1)" ;; esac
            fi ;;
        tail)
            if [ "${#ARGS[@]}" -gt 0 ]; then
                case "${ARGS[0]}" in ''|*[!0-9]*|0) _usage_error tail "N '${ARGS[0]}': expected a number of records (>= 1)" ;; esac
            fi
            if [ "${#ARGS[@]}" -gt 1 ]; then
                case "${ARGS[1]}" in snapshot|exits|node) ;; *) _usage_error tail "STREAM '${ARGS[1]}': expected snapshot, exits or node" ;; esac
            fi ;;
        help)
            if [ "${#ARGS[@]}" -gt 0 ]; then _spec "${ARGS[0]}" >/dev/null || _unknown_command "${ARGS[0]}"; fi ;;
    esac
}

# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------
cmd_bootstrap() {
    _bootstrap_plan
    if [ "$OPT_DRY_RUN" = 1 ]; then
        _bootstrap_show_plan
        echo "nothing was changed (--dry-run)"
        return 0
    fi
    need_root bootstrap
    echo "== bootstrap on $HOST ($(uname -r))"
    _bootstrap_apply
    _set_sysctls
    _forget_features
    echo "== re-checking"
    cmd_check
}

_forget_features() {
    # What bootstrap installs (BCC, kernel headers, bpftool) can load blocks the
    # last probe could not, so the next start probes every block afresh rather
    # than taking the feature cache's word for them.
    local f n=0
    for f in "$OUTPUT_DIR"/.features_*.json; do
        [ -e "$f" ] || continue
        rm -f "$f" && n=$((n+1))
    done
    ok "feature cache" "$n file(s) removed: the next start probes every block"
}

# What bootstrap would do, without doing it: the package manager, the packages
# missing, and the apt sources that lack `universe` (Ubuntu).
BOOT_MGR=""; BOOT_PKGS=(); BOOT_SOURCES=()
_bootstrap_plan() {
    local s
    BOOT_MGR=""; BOOT_PKGS=(); BOOT_SOURCES=()
    if command -v apt-get >/dev/null 2>&1; then
        BOOT_MGR=apt-get
        # BCC lives in `universe` on Ubuntu. A fresh CloudLab image can have it
        # disabled, which shows up as an empty apt-cache Candidate rather than an
        # error. Add the component to the EXISTING stanzas so we keep the machine's
        # own mirror and signing key, instead of add-apt-repository, which would
        # itself need software-properties-common installed first.
        for s in /etc/apt/sources.list.d/*.sources; do
            [ -f "$s" ] || continue
            if grep -qE '^Components:' "$s" && ! grep -qE '^Components:.*universe' "$s"; then
                BOOT_SOURCES+=("$s")
            fi
        done
        if [ -f /etc/apt/sources.list ] && grep -qE '^deb ' /etc/apt/sources.list \
                && ! grep -qE '^deb .*universe' /etc/apt/sources.list; then
            BOOT_SOURCES+=(/etc/apt/sources.list)
        fi
        python3 -c 'import bcc' 2>/dev/null || BOOT_PKGS+=(python3-bpfcc)
        [ -d "/lib/modules/$(uname -r)/build" ] || BOOT_PKGS+=("linux-headers-$(uname -r)")
        command -v bpftool >/dev/null 2>&1 || BOOT_PKGS+=("linux-tools-$(uname -r)" linux-tools-common)
        command -v ss >/dev/null 2>&1 || BOOT_PKGS+=(iproute2)
        command -v jq >/dev/null 2>&1 || BOOT_PKGS+=(jq)
    elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
        BOOT_MGR=dnf; command -v dnf >/dev/null 2>&1 || BOOT_MGR=yum
        python3 -c 'import bcc' 2>/dev/null || BOOT_PKGS+=(python3-bcc)
        [ -d "/lib/modules/$(uname -r)/build" ] || BOOT_PKGS+=("kernel-devel-$(uname -r)")
        command -v bpftool >/dev/null 2>&1 || BOOT_PKGS+=(bpftool)
        command -v ss >/dev/null 2>&1 || BOOT_PKGS+=(iproute)
        command -v jq >/dev/null 2>&1 || BOOT_PKGS+=(jq)
    fi
}

_bootstrap_show_plan() {
    local c cur
    echo "bootstrap on $HOST ($(uname -r)) would:"
    if [ -z "$BOOT_MGR" ]; then
        echo "  stop: no apt-get, dnf or yum here; install python3-bcc and kernel headers by hand"
    elif [ "${#BOOT_PKGS[@]}" -gt 0 ]; then
        echo "  install with $BOOT_MGR: ${BOOT_PKGS[*]}"
    else
        echo "  install nothing: every dependency is present"
    fi
    for c in "${BOOT_SOURCES[@]+"${BOOT_SOURCES[@]}"}"; do
        echo "  add the universe component to $c (a copy kept as $c.ebpfm.bak)"
    done
    for c in kernel.sched_schedstats kernel.task_delayacct; do
        cur="$(_sysctl_get "$c")"
        case "$cur" in
            1)          echo "  leave $c at 1" ;;
            missing)    echo "  skip $c: no such knob on this kernel (compile-time only)" ;;
            unreadable) echo "  set $c to 1 (this user cannot read its value)" ;;
            *)          echo "  set $c from $cur to 1" ;;
        esac
    done
    echo "  write $SYSCTL_CONF, so both stay set across reboots"
    echo "  delete the feature cache in $OUTPUT_DIR, then run check"
}

_bootstrap_apply() {
    local s
    case "$BOOT_MGR" in
        apt-get)
            for s in "${BOOT_SOURCES[@]+"${BOOT_SOURCES[@]}"}"; do
                cp -n "$s" "$s.ebpfm.bak" 2>/dev/null || true
                case "$s" in
                    */sources.list) sed -i -E 's/^(deb .*main.*)$/\1 universe/' "$s" ;;
                    *)              sed -i -E 's/^(Components:.*)$/\1 universe/' "$s" ;;
                esac
                echo "  + universe -> $s"
            done
            echo "  apt-get update"
            DEBIAN_FRONTEND=noninteractive apt-get update -qq || warn apt-update "non-zero; continuing"
            if [ "${#BOOT_PKGS[@]}" -eq 0 ]; then
                echo "  all dependencies already present"
            else
                echo "  installing: ${BOOT_PKGS[*]}"
                DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${BOOT_PKGS[@]}" \
                    || die "apt-get install failed (universe enabled? ${#BOOT_SOURCES[@]} source(s) changed)"
            fi ;;
        dnf|yum)
            if [ "${#BOOT_PKGS[@]}" -eq 0 ]; then
                echo "  all dependencies already present"
            else
                echo "  installing: ${BOOT_PKGS[*]}"
                "$BOOT_MGR" install -y "${BOOT_PKGS[@]}" || die "$BOOT_MGR install failed"
            fi ;;
        *) die "no apt-get/dnf/yum; install python3-bcc and kernel headers by hand" ;;
    esac
}

_set_sysctls() {
    # Without these two, item 2 (uninterruptible-sleep stall time) and the
    # blkio/swapin dwell fields are silently zero rather than absent, which is the
    # worst possible failure for a measurement tool.
    local conf="$SYSCTL_CONF"
    echo "== sysctls (required: two data points are inert without them)"
    local k v
    for k in kernel.sched_schedstats kernel.task_delayacct; do
        v="$(_sysctl_get "$k")"
        if [ "$v" = missing ]; then
            warn "$k" "no such knob on this kernel (compile-time only)"
        elif [ "$v" = 1 ]; then
            ok "$k" "already 1"
        else
            sysctl -w "$k"=1 >/dev/null 2>&1 && ok "$k" "0 -> 1" || bad "$k" "could not set" "sysctl -w $k=1"
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
    fails=0; CK_FAILS_NODE=(); CK_FAILS_EBPF=()
    local K CFG
    K="$(uname -r)"; CFG="/boot/config-$K"
    [ -r "$CFG" ] || { [ -r /proc/config.gz ] && CFG=/proc/config.gz; }
    _cfg() {
        if [ "$CFG" = /proc/config.gz ]; then zcat "$CFG" 2>/dev/null | grep -E "^$1=" | cut -d= -f2
        elif [ -r "$CFG" ]; then grep -E "^$1=" "$CFG" 2>/dev/null | cut -d= -f2; fi
    }
    echo "== ebpfm check on $HOST (kernel $K)  [tier: $TIER]"

    printf -- '-- privilege\n'
    CK_TIER=ebpf
    if [ "$IS_ROOT" = 1 ]; then
        ok euid "0 (root) -- both tiers available"
    elif want_ebpf; then
        bad euid "$(id -u): the ebpf tier needs root" "sudo $HINT check   (or EBPFM_TIER=node: the node tier alone)"
    else
        ok euid "$(id -u) (unprivileged) -- node tier only"
        printf '        skipping the ebpf tier: per-process exits, exit codes, argv, cwd,\n'
        printf '        all-user per-process I/O, D-state dwell, net bytes, TCP records.\n'
        printf '        There is no unprivileged substitute; re-run under sudo for those.\n'
    fi
    local ld; ld="$(cat /sys/kernel/security/lockdown 2>/dev/null | grep -o '\[[a-z]*\]')"
    case "$ld" in
        "[confidentiality]") bad lockdown "$ld blocks bpf_probe_read" \
                                 "boot with lockdown=integrity or none, or run the node tier alone (EBPFM_TIER=node)" ;;
        "") ok lockdown "not enforced" ;;
        *) ok lockdown "$ld" ;;
    esac

    printf -- '-- settings\n'
    CK_TIER=both
    _check_settings

    if want_node; then CK_TIER=node; _check_node_tier; fi
    if ! want_ebpf; then
        printf -- '-- vendored libraries\n'
        _check_vendored
        _check_end
        return "$fails"
    fi
    CK_TIER=ebpf

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
    if command -v systemd-run >/dev/null 2>&1 && _systemd_booted; then
        ok systemd-run "$(command -v systemd-run)"
    elif command -v systemd-run >/dev/null 2>&1; then
        warn systemd-run "present, but systemd is not running here: 'start' uses nohup inside this cgroup"
    else
        warn systemd-run "absent; 'start' falls back to nohup inside this cgroup"
    fi

    printf -- '-- toolchain\n'
    if python3 -c 'import bcc' 2>/dev/null; then
        ok python3-bcc "$(python3 -c 'import bcc;print(getattr(bcc,"__version__","?"))' 2>/dev/null)"
    else
        bad python3-bcc "not importable" "sudo $HINT bootstrap"
    fi
    [ -d "/lib/modules/$K/build" ] && ok "kernel headers" "/lib/modules/$K/build" \
        || warn "kernel headers" "missing (BCC needs them unless it can use BTF; bootstrap installs them)"
    [ -r /sys/kernel/btf/vmlinux ] && ok BTF "$(stat -c %s /sys/kernel/btf/vmlinux) bytes" \
        || warn BTF "absent — tcp_btf and tcp_accept will be dropped"
    command -v bpftool >/dev/null 2>&1 && ok bpftool "$(command -v bpftool)" \
        || warn bpftool "absent — no BTF offsets, no overhead measurement"
    command -v ss >/dev/null 2>&1 && ok ss "$(command -v ss)" || warn ss "absent (netlink is used directly anyway)"

    printf -- '-- kernel config\n'
    local c v
    for c in CONFIG_BPF_SYSCALL CONFIG_BPF_EVENTS CONFIG_FTRACE_SYSCALLS; do
        v="$(_cfg $c)"; [ "$v" = y ] && ok "$c" y \
            || bad "$c" "${v:-?}" "this kernel cannot run the eBPF tier; the node tier runs alone (EBPFM_TIER=node)"
    done
    for c in CONFIG_TASK_IO_ACCOUNTING CONFIG_TASK_XACCT CONFIG_SCHED_INFO CONFIG_SCHEDSTATS \
             CONFIG_TASK_DELAY_ACCT CONFIG_DEBUG_INFO_BTF; do
        v="$(_cfg $c)"; [ "$v" = y ] && ok "$c" y || warn "$c" "${v:-?} (block dropped or degraded)"
    done

    printf -- '-- sysctls\n'
    for c in kernel.sched_schedstats kernel.task_delayacct; do
        v="$(_sysctl_get "$c")"
        case "$v" in
            1) ok "$c" 1 ;;
            missing) warn "$c" "no such knob (compile-time only)" ;;
            unreadable) warn "$c" "unreadable as this user" ;;
            *) bad "$c" "$v — dstate/blkio fields will read 0" "sudo $HINT bootstrap   (or: sysctl -w $c=1)" ;;
        esac
    done

    printf -- '-- tracepoints\n'
    local T=/sys/kernel/tracing/events tfix
    [ -d "$T" ] || T=/sys/kernel/debug/tracing/events
    tfix="this kernel lacks the sched tracepoints; the node tier runs alone (EBPFM_TIER=node)"
    if [ ! -d "$T" ] && grep -qw tracefs /proc/filesystems 2>/dev/null; then
        tfix="tracefs is not mounted: mount -t tracefs nodev /sys/kernel/tracing"
    fi
    local tp
    for tp in sched/sched_process_fork sched/sched_process_exec sched/sched_process_exit; do
        [ -d "$T/$tp" ] && ok "$tp" present || bad "$tp" "MISSING (core)" "$tfix"
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
    local nl nlerr
    nl="$(mktemp)"; nlerr="$(mktemp)"
    if python3 -c "import sys; sys.path.insert(0,'$DIR/lib'); import sockdiag; r=sockdiag.dump(); print(len(r))" \
            >"$nl" 2>"$nlerr"; then
        ok sock_diag "$(cat "$nl") sockets enumerated"
    else
        warn sock_diag "unreachable: $(head -1 "$nlerr" 2>/dev/null)"
    fi
    rm -f "$nl" "$nlerr"

    printf -- '-- other-user /proc reads\n'
    local P
    P="$(ps -eo pid,uid --no-headers 2>/dev/null | awk '$2>=1000 && $2<60000 {print $1; exit}')"
    if [ -n "${P:-}" ]; then
        head -1 "/proc/$P/io" >/dev/null 2>&1 && ok "/proc/<other>/io" "readable (pid $P)" \
            || bad "/proc/<other>/io" "EACCES for pid $P" \
                   "run as root; check /proc's hidepid= mount option and kernel.yama.ptrace_scope"
        readlink "/proc/$P/cwd" >/dev/null 2>&1 && ok "/proc/<other>/cwd" "readlink ok" \
            || warn "/proc/<other>/cwd" "unreadable"
    else
        warn "/proc/<other>" "no non-root user process to test against"
    fi

    printf -- '-- eBPF output\n'
    local d="$OUTPUT_DIR" df_line
    while [ ! -d "$d" ] && [ "$d" != / ]; do d="$(dirname "$d")"; done
    if [ -w "$d" ]; then
        df_line="$(df -Ph "$d" 2>/dev/null | awk 'NR == 2 {printf "%s free (%d%%)", $4, 100 - $5}')"
        ok "ebpf output" "$OUTPUT_DIR (writable${df_line:+; $df_line})"
        case "$(df -P "$d" 2>/dev/null | awk 'NR == 2 {print ($5 + 0 > 90) ? "low" : "ok"}')" in
            low) warn "ebpf output space" "under 10% free: the exits stream can pass 1 GB a day" ;;
        esac
    else
        bad "ebpf output" "$OUTPUT_DIR is not writable" \
            "set EBPFM_OUTPUT_DIR to a folder root can write ($PROG config shows where it is set)"
    fi

    printf -- '-- vendored libraries\n'
    _check_vendored

    printf -- '-- python syntax\n'
    python3 -B -c 'import sys
for p in sys.argv[1:]:
    compile(open(p, "rb").read(), p, "exec")' "$TRACER" "$DIR"/lib/*.py 2>/dev/null && ok py_compile "all modules" \
        || bad py_compile "syntax error" "reinstall the collector: this copy is damaged or incomplete"

    _check_end
    return "$fails"
}

# Each setting both tiers read: a value one cannot use is a FAIL with where it is
# set; a name that is no setting is a WARN with the nearest one.
_check_settings() {
    local out line name n=0
    if ! command -v python3 >/dev/null 2>&1; then
        bad python3 "not found: both tiers need it" "install python3 (3.6 or later)"
        return 0
    fi
    out="$(_settings_py validate "${SETTINGS_ARGS[@]}" 2>&1)"
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        line="${line#ebpfm: }"
        case "$line" in
            warning:*) line="${line#warning: }"; warn "${line%% *}" "${line#* }" ;;
            *) n=$((n + 1)); name="${line%%=*}"
               bad "$name" "${line#*=}" "correct or remove it ($PROG config shows where each setting comes from)" ;;
        esac
    done <<< "$out"
    if [ "$n" = 0 ]; then
        case "$_FILE_STATE" in
            read) ok settings "valid; ${#_FROM_FILE[@]} from $DEFAULT_FILE" ;;
            *)    ok settings "valid; $(_file_label): ${_FILE_STATE/none/not present}" ;;
        esac
    fi
}

_check_end() {
    printf '== %d FAIL\n' "$fails"
    [ "$CK_NO_VERDICT" = 1 ] || _verdict
}

# READY: both tiers / READY: node tier only / NOT READY: N FAIL, each FAIL with
# its fix. Returns 0 only when every tier this user runs is ready.
_verdict() {
    local n_node="${#CK_FAILS_NODE[@]}" n_ebpf="${#CK_FAILS_EBPF[@]}" e
    _verdict_list() {
        local entry name msg fix
        for entry in "$@"; do
            IFS="$_US" read -r name msg fix <<< "$entry"
            printf '  - %s: %s\n' "$name" "$msg"
            [ -n "$fix" ] && printf '      fix: %s\n' "$fix"
        done
        return 0
    }
    if want_node && [ "$n_node" -gt 0 ]; then
        echo "NOT READY: $((n_node + n_ebpf)) FAIL"
        _verdict_list "${CK_FAILS_NODE[@]}" "${CK_FAILS_EBPF[@]+"${CK_FAILS_EBPF[@]}"}"
        return 1
    fi
    if want_ebpf && [ "$n_ebpf" -gt 0 ]; then
        if want_node; then echo "READY: node tier only (the eBPF tier is NOT READY: $n_ebpf FAIL)"
        else echo "NOT READY: $n_ebpf FAIL"; fi
        _verdict_list "${CK_FAILS_EBPF[@]}"
        return 1
    fi
    if want_node && want_ebpf; then e="READY: both tiers"
    elif want_node; then e="READY: node tier only"
    else e="READY: eBPF tier only"; fi
    echo "$e"
    return 0
}

_check_node_tier() {
    printf -- '-- node tier (unprivileged)\n'
    [ -f "$NODE_SNAPSHOT" ] && ok node_snapshot "$NODE_SNAPSHOT" \
        || bad node_snapshot "missing ($NODE_SNAPSHOT)" "copy the whole collector/ folder: this copy is incomplete"
    if [ -f "$NODE_SNAPSHOT" ]; then
        # compile without writing __pycache__: a user cannot write the install folder
        python3 -B -c 'import sys
compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' "$NODE_SNAPSHOT" 2>/dev/null \
            && ok "node_snapshot syntax" ok \
            || bad "node_snapshot syntax" "does not compile" "reinstall the collector: this copy is damaged"
    fi
    # The node tier masks argv with the lib/redact.py beside it. One it cannot
    # import leaves every command line [REDACTED]; one that masks nothing, raw.
    if python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import redact; out = redact.redact_line("tool --password hunter2check")[0]; sys.exit(0 if "hunter2check" not in out and redact.MASK in out else 1)' \
            "$(dirname "$NODE_SNAPSHOT")/lib" >/dev/null 2>&1; then
        ok redact "masks a test credential"
    else
        bad redact "$(dirname "$NODE_SNAPSHOT")/lib/redact.py did not mask a test credential" \
            "reinstall lib/redact.py from the same release as the rest"
    fi
    ok "snapshot mode" "$(_node_mode)$(want_ebpf && echo '  (ebpf tier carries the 4 dropped sections)' || echo '  (no ebpf tier: keeping every section)')"

    # snapshot.py shells out to these. A missing one degrades one section to
    # empty rather than failing, so they are warnings -- except ps, without
    # which most of the per-process sections are empty.
    local t pkg
    for t in ps; do
        command -v "$t" >/dev/null 2>&1 && ok "$t" "$(command -v $t)" || bad "$t" "absent" "install procps (procps-ng on EL)"
    done
    for t in mpstat iostat sar nfsiostat free ss uptime w; do
        case "$t" in
            mpstat|iostat|sar) pkg=sysstat ;;
            nfsiostat)         pkg="nfs-utils (EL) or nfs-common (Debian, Ubuntu)" ;;
            ss)                pkg="iproute (EL) or iproute2" ;;
            *)                 pkg="procps-ng (EL) or procps" ;;
        esac
        command -v "$t" >/dev/null 2>&1 && ok "$t" "$(command -v $t)" \
            || warn "$t" "absent -> that section will be empty (install $pkg)"
    done

    [ -r /proc/stat ] && ok /proc/stat "btime=$(awk '/^btime/{print $2}' /proc/stat 2>/dev/null)" \
        || bad /proc/stat "unreadable -- btime is the reboot-era key for EVERY dataset" "mount /proc"
    [ -d /sys/fs/cgroup/user.slice ] && ok cgroup-v2 "user.slice present" \
        || warn cgroup-v2 "no user.slice -> per_user_cgroup will be empty"

    # Writable, or creatable: the nearest folder that exists on its way can be written.
    local d="$NODE_OUTPUT_DIR"
    while [ ! -d "$d" ] && [ "$d" != / ]; do d="$(dirname "$d")"; done
    if [ -w "$d" ]; then
        ok "node output" "$NODE_OUTPUT_DIR (writable)"
    else
        bad "node output" "$NODE_OUTPUT_DIR not writable" "set EBPFM_NODE_OUTPUT_DIR to a folder this user can write"
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

# One node-tier snapshot appended to today's file. Unprivileged.
# node_snapshot.py prints one JSON line. It is captured first, so a snapshot the
# timeout kills appends nothing rather than half a line; then the line is
# appended to $HOST.jsonl. node_snapshot.py itself is vendored and stays as it
# is (VENDORED.md).
_node_tick() {
    local d out
    d="$NODE_OUTPUT_DIR/$(date +%F)"
    _secure_dir "$d" || return 1
    # Bounded: one snapshot stuck on a hung mount would otherwise stall every
    # later one. The next tick simply tries again.
    out="$(SNAPSHOT_MODE="$(_node_mode)" $(_node_timeout) python3 "$NODE_SNAPSHOT" \
        "$HOST" "$NODE_TOP_N")" || return 1
    [ -n "$out" ] || return 1
    # The file is created and given the configured mode/group before the record
    # lands, so no snapshot ever sits in a file at the umask's mode.
    { : >> "$d/$HOST.jsonl"; } 2>/dev/null; _secure_file "$d/$HOST.jsonl"
    printf '%s\n' "$out" >> "$d/$HOST.jsonl" || return 1
}

# `timeout` with a limit just under the interval, when coreutils has it
_node_timeout() {
    command -v timeout >/dev/null 2>&1 || return 0
    local lim=$((NODE_INTERVAL - 10))
    [ "$lim" -lt 30 ] && lim=30
    printf 'timeout -k 10 %s' "$lim"
}

# The node tier's loop, under `start` (nohup) or ebpfm-node.service. It refuses
# settings it cannot use (exit 2: the unit does not restart it), notes its
# settings for `status`, says when a snapshot fails, and on SIGTERM finishes the
# snapshot it is taking -- appended whole or not at all -- then exits.
_node_loop() {
    _require_valid_settings _node_daemon
    local log="${_EBPFM_LOG:-}" rc
    if [ -z "$log" ]; then
        if [ -n "${INVOCATION_ID:-}" ]; then log="journal:${NAME}-node.service"; else log="$NODE_LOG"; fi
    fi
    mkdir -p "$PID_DIR" 2>/dev/null
    _write_state "$NODE_STATE_FILE" EBPFM_STATE_PID="$$" EBPFM_STATE_STARTED="$(date +%s)" \
        EBPFM_STATE_VERSION="$(_version)" EBPFM_STATE_LOG="$log" \
        EBPFM_NODE_OUTPUT_DIR="$NODE_OUTPUT_DIR" EBPFM_NODE_INTERVAL="$NODE_INTERVAL" \
        EBPFM_NODE_TOP_N="$NODE_TOP_N" EBPFM_PID_DIR="$PID_DIR" SNAPSHOT_MODE="$(_node_mode)"
    trap '_node_loop_end' TERM INT
    while true; do
        _node_tick; rc=$?
        if [ "$rc" != 0 ]; then
            printf '%s ebpfm node tier: snapshot failed (exit %s); the next is due in %ss\n' \
                "$(date '+%F %T')" "$rc" "$NODE_INTERVAL" >&2
        fi
        sleep "$NODE_INTERVAL" &
        wait $! 2>/dev/null
    done
}
_node_loop_end() {
    local j
    for j in $(jobs -p); do kill "$j" 2>/dev/null; done
    _state_is "$NODE_STATE_FILE" "$$" && rm -f "$NODE_STATE_FILE"
    exit 0
}

cmd_once() {
    local dur="${1:-${EBPFM_DURATION:-30}}"
    if want_node; then
        echo "node tier: one snapshot (mode=$(_node_mode)) -> $NODE_OUTPUT_DIR/$(date +%F)/$HOST.jsonl"
        _node_tick && echo "  ok" || echo "  FAILED"
    fi
    if want_ebpf; then
        need_root once
        _secure_dir "$OUTPUT_DIR"
        echo "ebpf tier: capturing ${dur}s on $HOST -> $OUTPUT_DIR/$(date +%F)/$HOST.{snapshot,exits}.jsonl"
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

# Whether PID is a live process. A zombie -- exited, not yet reaped -- has stopped,
# though kill -0 still finds it: a tier `start` left behind is reaped by PID 1, and in
# a container whose PID 1 reaps nothing (CI's `tail -f /dev/null`) it stays a zombie.
_alive() {   # _alive PID
    kill -0 "$1" 2>/dev/null || return 1
    local st
    st="$(sed -n 's/^.*) \(.\).*$/\1/p' "/proc/$1/stat" 2>/dev/null)"
    [ "$st" != Z ]
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

# --- small helpers -----------------------------------------------------------------
_size() { stat -c %s "$1" 2>/dev/null || echo 0; }
# A kernel knob's value, read from /proc/sys (sysctl(8) is in sbin, off many
# users' PATH): its value, `missing` (no such knob) or `unreadable`.
_sysctl_get() {   # KEY, e.g. kernel.task_delayacct
    local f="/proc/sys/${1//.//}"
    [ -e "$f" ] || { echo missing; return 0; }
    cat "$f" 2>/dev/null || echo unreadable
}
_elapsed() {   # PID -> seconds it has run, or nothing
    case "${1:-}" in ''|0|*[!0-9]*) return 0 ;; esac
    ps -o etimes= -p "$1" 2>/dev/null | tr -d ' '
}
# sd_booted(3)'s test: systemd runs this machine (EBPFM_SYSTEMD_DIR stands in, for tests)
_systemd_booted() { [ -d "${EBPFM_SYSTEMD_DIR:-/run/systemd/system}" ]; }
# StandardOutput=append: arrived in systemd 240; older ones log to the journal.
_systemd_appends() {
    local v; v="$(systemctl --version 2>/dev/null | awk 'NR == 1 {print $2}')"
    case "$v" in ''|*[!0-9]*) return 1 ;; esac
    [ "$v" -ge 240 ]
}
# What LOG received since OFF: bytes for a file; for journal:UNIT, since epoch OFF.
_log_since() {   # LOG OFF
    case "$1" in
        journal:*) journalctl -u "${1#journal:}" --since "@$2" --no-pager -o cat 2>/dev/null ;;
        *) [ -f "$1" ] && tail -c +"$(( ${2:-0} + 1 ))" "$1" 2>/dev/null ;;
    esac
    return 0
}
_log_tail() {   # LOG OFF: its last lines since OFF, indented
    local lines
    lines="$(_log_since "$1" "$2" | tail -n 10)"
    if [ -n "$lines" ]; then
        printf '  the log (%s) says:\n' "$(_log_name "$1")"
        printf '%s\n' "$lines" | sed 's/^/    /'
    else
        printf '  the log (%s) has nothing new\n' "$(_log_name "$1")"
    fi
}
_log_name() { case "$1" in journal:*) echo "journalctl -u ${1#journal:}" ;; *) echo "$1" ;; esac; }

# --- the running instance: how each tier runs, and with which settings -----------
# A tier keeps a note of its settings beside its pid file: `start` writes the eBPF
# tier's, and the node tier's loop its own, under its unit too. A later `status`
# or `tail` -- from a fresh shell, or once /scratch has mounted -- reads where the
# running tier writes, not where this shell would. KEY=VALUE lines, the last of a
# key winning; written whole (renamed into place), so never read half-written.
_write_state() {   # FILE KEY=VALUE...
    local f="$1"
    shift
    { env | grep -E '^(EBPFM|SNAPSHOT)_[A-Za-z0-9_]*=' | grep -v '^EBPFM_STATE_'
      printf '%s\n' "$@"; } > "$f.tmp.$$" 2>/dev/null && mv -f "$f.tmp.$$" "$f" 2>/dev/null
    rm -f "$f.tmp.$$" 2>/dev/null
    return 0
}
_state_get() { sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -n 1; }
_state_is() { [ -r "$1" ] && [ -n "${2:-}" ] && [ "$(_state_get "$1" EBPFM_STATE_PID)" = "$2" ]; }

# VAR as UNIT's process has it: its environment (root can read it), else the
# settings file (systemd lets it win), else the unit's own Environment=.
_unit_env() {   # VAR UNIT
    local var="$1" u="$2" pid v
    pid="$(systemctl show -p MainPID --value "$u" 2>/dev/null)"
    case "$pid" in
        ''|0|*[!0-9]*) ;;
        *) if [ -r "/proc/$pid/environ" ]; then
               v="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n "s/^$var=//p" | tail -n 1)"
               [ -n "$v" ] && { printf '%s' "$v"; return 0; }
           fi ;;
    esac
    if [ "${_FILE_VALS[$var]+set}" = set ]; then printf '%s' "${_FILE_VALS[$var]}"; return 0; fi
    systemctl show -p Environment --value "$u" 2>/dev/null | tr ' ' '\n' | sed -n "s/^$var=//p" | tail -n 1
}
_unit_pid_dir() { local v; v="$(_unit_env EBPFM_PID_DIR "$1")"; printf '%s' "${v:-/run}"; }

NI_HOW=""; NI_UNIT=""; NI_PID=""; NI_STATE=""
EI_HOW=""; EI_UNIT=""; EI_PID=""; EI_STATE=""
INSTANCE_NOTES_NODE=""; INSTANCE_NOTES_EBPF=""
_node_instance() {
    local u p
    NI_HOW=""; NI_UNIT=""; NI_PID=""; NI_STATE=""
    if u="$(_node_active_unit)"; then
        NI_HOW=unit; NI_UNIT="$u"; NI_PID="$(systemctl show -p MainPID --value "$u" 2>/dev/null)"
        NI_STATE="$(_unit_pid_dir "$u")/${NAME}-node_${HOST}.env"
    elif p="$(_node_running_pid)"; then
        NI_HOW=pid; NI_PID="$p"; NI_STATE="$NODE_STATE_FILE"
    fi
    _state_is "$NI_STATE" "$NI_PID" || NI_STATE=""
    return 0
}
_ebpf_instance() {
    local u p
    EI_HOW=""; EI_UNIT=""; EI_PID=""; EI_STATE=""
    if u="$(_active_unit)"; then
        EI_HOW=unit; EI_UNIT="$u"; EI_PID="$(systemctl show -p MainPID --value "$u" 2>/dev/null)"
        [ "$u" = "${NAME}-${HOST}.service" ] && EI_STATE="$STATE_FILE"
    elif p="$(_running_pid)"; then
        EI_HOW=pid; EI_PID="$p"; EI_STATE="$STATE_FILE"
    fi
    _state_is "$EI_STATE" "$EI_PID" || EI_STATE=""
    return 0
}

# Where, how often and to which log each running tier writes, in place of what
# this shell resolves; a difference is noted for `status`.
_adopt_instances() {
    local v shell_out="$OUTPUT_DIR" shell_node="$NODE_OUTPUT_DIR"
    _node_instance
    _ebpf_instance
    if [ -n "$NI_STATE" ]; then
        v="$(_state_get "$NI_STATE" EBPFM_NODE_OUTPUT_DIR)"; [ -n "$v" ] && NODE_OUTPUT_DIR="$v"
        v="$(_state_get "$NI_STATE" EBPFM_NODE_INTERVAL)"; [ -n "$v" ] && NODE_INTERVAL="$v"
        v="$(_state_get "$NI_STATE" EBPFM_STATE_LOG)"; [ -n "$v" ] && NODE_LOG="$v"
    elif [ "$NI_HOW" = unit ]; then
        v="$(_unit_env EBPFM_NODE_OUTPUT_DIR "$NI_UNIT")"; [ -n "$v" ] && NODE_OUTPUT_DIR="$v"
        v="$(_unit_env EBPFM_NODE_INTERVAL "$NI_UNIT")"; [ -n "$v" ] && NODE_INTERVAL="$v"
    fi
    [ "$NI_HOW" = unit ] && NODE_LOG="journal:$NI_UNIT"
    if [ -n "$EI_STATE" ]; then
        v="$(_state_get "$EI_STATE" EBPFM_OUTPUT_DIR)"; [ -n "$v" ] && OUTPUT_DIR="$v"
        v="$(_state_get "$EI_STATE" EBPFM_RESIDENCY_S)"; [ -n "$v" ] && EBPF_INTERVAL="$v"
        v="$(_state_get "$EI_STATE" EBPFM_STATE_LOG)"; [ -n "$v" ] && LOG="$v"
    elif [ "$EI_HOW" = unit ]; then
        v="$(_unit_env EBPFM_OUTPUT_DIR "$EI_UNIT")"; [ -n "$v" ] && OUTPUT_DIR="$v"
        v="$(_unit_env EBPFM_RESIDENCY_S "$EI_UNIT")"; [ -n "$v" ] && EBPF_INTERVAL="$v"
        LOG="journal:$EI_UNIT"
    fi
    if [ -n "$NI_HOW" ] && [ "$NODE_OUTPUT_DIR" != "$shell_node" ]; then
        INSTANCE_NOTES_NODE="it writes to $NODE_OUTPUT_DIR; this shell's settings name $shell_node"
    fi
    if [ -n "$EI_HOW" ] && [ "$OUTPUT_DIR" != "$shell_out" ]; then
        INSTANCE_NOTES_EBPF="it writes to $OUTPUT_DIR; this shell's settings name $shell_out"
    fi
    return 0
}

# --- start ---------------------------------------------------------------------
# The node tier is a plain nohup poll loop guarded by a pid file -- the same
# contract as collect/common/daemon.sh, so a supervisor may call `start` every
# cycle: an already-running daemon is left strictly alone, and `start` says so
# with exit status 0. It needs no systemd and no cgroup escape; one snapshot is a
# few hundred ms of /proc reads.
_node_start() {   # [MODE]
    local p u mode="${1:-}" f0 s0 l0 np
    [ -n "$mode" ] || mode="$(_node_mode)"
    if u="$(_node_active_unit)"; then
        echo "node tier already running on $HOST (unit $u) -- not starting a second loop"
        return 0
    fi
    if p="$(_node_running_pid)"; then
        echo "node tier already running on $HOST (pid $p)"
        return 0
    fi
    rm -f "$NODE_PID_FILE"
    if [ ! -f "$NODE_SNAPSHOT" ]; then
        echo "node tier NOT started: $NODE_SNAPSHOT is missing" >&2
        return 1
    fi
    _secure_dir "$NODE_OUTPUT_DIR" || {
        echo "node tier NOT started: cannot create $NODE_OUTPUT_DIR; set EBPFM_NODE_OUTPUT_DIR to a folder you can write ($PROG config EBPFM_NODE_OUTPUT_DIR shows where it is set)" >&2
        return 1; }
    mkdir -p "$PID_DIR" || { echo "node tier NOT started: cannot create $PID_DIR" >&2; return 1; }
    f0="$NODE_OUTPUT_DIR/$(date +%F)/$HOST.jsonl"; s0="$(_size "$f0")"; l0="$(_size "$NODE_LOG")"
    # The folders, interval and mode this start resolved go to the loop as they
    # are, so it writes where this command says even if /scratch mounts later.
    EBPFM_NODE_OUTPUT_DIR="$NODE_OUTPUT_DIR" EBPFM_PID_DIR="$PID_DIR" EBPFM_NODE_INTERVAL="$NODE_INTERVAL" \
        EBPFM_NODE_TOP_N="$NODE_TOP_N" EBPFM_HOST="$HOST" _EBPFM_LOG="$NODE_LOG" SNAPSHOT_MODE="$mode" \
        nohup "$SELF" _node_daemon >>"$NODE_LOG" 2>&1 &
    np=$!
    # Confirm the child SURVIVED before claiming success. $! is the nohup
    # process, which exits immediately if it could not exec -- so without this
    # `start` cheerfully reported a pid that was already gone, and only `status`
    # (if anyone looked) revealed the node tier had never run.
    sleep 0.3
    if ! kill -0 "$np" 2>/dev/null; then
        echo "node tier FAILED to start on $HOST -- see $NODE_LOG" >&2
        _log_tail "$NODE_LOG" "$l0" >&2
        rm -f "$NODE_PID_FILE"
        return 1
    fi
    echo "$np" > "$NODE_PID_FILE"
    echo "node tier started on $HOST (pid $np, mode=$mode, every ${NODE_INTERVAL}s)"
    echo "  output: $NODE_OUTPUT_DIR/<date>/$HOST.jsonl"
    echo "  log:    $NODE_LOG"
    if [ "$OPT_NO_WAIT" = 1 ]; then
        echo "  not waiting for its first snapshot (--no-wait): $PROG status shows when it lands"
        return 0
    fi
    _await_node "$np" "$f0" "$s0" "$NODE_LOG" "$l0"
}

# Until the node tier's first snapshot lands: 0 it did; 1 the loop died or the
# snapshot failed (the log says why; a failing loop keeps trying, as it would at
# 3 a.m.); 4 neither within the wait.
_await_node() {   # PID FILE0 SIZE0 LOG LOG0
    local pid="$1" f0="$2" s0="$3" log="$4" l0="$5" i f
    printf '  waiting for its first snapshot (up to %ss) ...' "$OPT_WAIT"
    for ((i = 0; i < OPT_WAIT * 2; i++)); do
        f="$NODE_OUTPUT_DIR/$(date +%F)/$HOST.jsonl"
        if { [ "$f" != "$f0" ] && [ "$(_size "$f")" -gt 0 ]; } || [ "$(_size "$f")" -gt "$s0" ]; then
            echo " ok"
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            echo " FAILED: the loop exited"
            _log_tail "$log" "$l0"
            rm -f "$NODE_PID_FILE"
            return 1
        fi
        if _log_since "$log" "$l0" | grep -q 'snapshot failed'; then
            echo " FAILED: its first snapshot failed"
            _log_tail "$log" "$l0"
            echo "  the loop keeps trying every ${NODE_INTERVAL}s (pid $pid); $PROG stop stops it"
            return 1
        fi
        sleep 0.5
    done
    echo " not yet: no snapshot after ${OPT_WAIT}s, though the loop runs (pid $pid)"
    _log_tail "$log" "$l0"
    echo "  $PROG status says when one lands; $PROG stop stops it"
    return 4
}

_ebpf_start() {
    need_root start
    local p u mp="" unit="${NAME}-${HOST}.service" l0 log="$LOG" v i
    local setenv=() out_props=()
    # Two collectors on one node would double every record and fight over the
    # day file, so defer to an already-active unit instead of adding a second.
    if u="$(_active_unit)"; then
        echo "$NAME already running on $HOST (unit $u) -- not starting a second collector"
        return 0
    fi
    if p="$(_running_pid)"; then echo "$NAME already running on $HOST (pid $p)"; return 0; fi
    rm -f "$PID_FILE" "$STATE_FILE"
    _secure_dir "$OUTPUT_DIR" || { echo "ebpf tier NOT started: cannot create $OUTPUT_DIR" >&2; return 1; }
    mkdir -p "$PID_DIR"
    l0="$(_size "$LOG")"
    for v in $(compgen -e | grep -E '^EBPFM_[A-Za-z0-9_]*$'); do
        [ "$v" = EBPFM_OUTPUT_DIR ] && continue
        setenv+=("--setenv=$v=${!v}")
    done
    if command -v systemd-run >/dev/null 2>&1 && _systemd_booted; then
        # into system.slice, so the drain is not charged to a capped user slice
        systemctl reset-failed "$unit" >/dev/null 2>&1
        if _systemd_appends; then
            out_props=(--property=StandardOutput=append:"$LOG" --property=StandardError=append:"$LOG")
        else
            log="journal:$unit"; l0="$(date +%s)"
        fi
        systemd-run --quiet --unit="${NAME}-${HOST}" --slice=system.slice \
            --property=Nice=19 "${out_props[@]+"${out_props[@]}"}" \
            --setenv=EBPFM_OUTPUT_DIR="$OUTPUT_DIR" --setenv=EBPFM_HOST="$HOST" \
            "${setenv[@]+"${setenv[@]}"}" \
            /usr/bin/env python3 "$TRACER" "$HOST" \
            || { echo "ebpf tier NOT started: systemd-run failed (its message is above)" >&2; return 1; }
        # MainPID once the unit has forked; a unit that died at once never has one
        for ((i = 0; i < 30; i++)); do
            mp="$(systemctl show -p MainPID --value "$unit" 2>/dev/null)"
            case "$mp" in ''|0|*[!0-9]*) mp="" ;; *) break ;; esac
            systemctl is-active --quiet "$unit" 2>/dev/null || break
            sleep 0.1
        done
        if [ -z "$mp" ]; then
            echo "ebpf tier FAILED to start on $HOST: unit $unit exited at once" >&2
            _log_tail "$log" "$l0" >&2
            return 1
        fi
        echo "$mp" > "$PID_FILE"
        echo "$NAME started on $HOST as unit $unit (pid $mp)"
        echo "  cgroup: $(systemctl show -p ControlGroup --value "$unit" 2>/dev/null)"
    else
        if command -v systemd-run >/dev/null 2>&1; then
            warn systemd-run "present, but systemd is not running here — starting with nohup INSIDE the current cgroup"
        else
            warn systemd-run "absent — starting with nohup INSIDE the current cgroup"
        fi
        EBPFM_HOST="$HOST" nohup nice -n 19 python3 "$TRACER" "$HOST" >>"$LOG" 2>&1 &
        mp=$!
        echo "$mp" > "$PID_FILE"
        echo "$NAME started on $HOST (pid $mp)"
    fi
    _write_state "$STATE_FILE" EBPFM_STATE_PID="$mp" EBPFM_STATE_STARTED="$(date +%s)" \
        EBPFM_STATE_VERSION="$(_version)" EBPFM_STATE_LOG="$log" \
        EBPFM_OUTPUT_DIR="$OUTPUT_DIR" EBPFM_RESIDENCY_S="$EBPF_INTERVAL"
    echo "  log:    $(_log_name "$log")"
    echo "  output: $OUTPUT_DIR/<date>/$HOST.{snapshot,exits}.jsonl"
    if [ "$OPT_NO_WAIT" = 1 ]; then
        sleep 1
        if ! kill -0 "$mp" 2>/dev/null; then
            echo "ebpf tier FAILED: it exited within a second" >&2
            _log_tail "$log" "$l0" >&2
            rm -f "$PID_FILE" "$STATE_FILE"
            return 1
        fi
        echo "  not waiting for it to attach (--no-wait): $PROG status shows when it records"
        return 0
    fi
    _await_ebpf "$mp" "$log" "$l0"
}

# Until the tracer's `meta` record lands -- written once its probes are attached
# and /proc is seeded: 0 it did; 1 the tracer exited (the log says why); 4 still
# starting after the wait (the first probe on a kernel compiles for ~40 s).
_await_ebpf() {   # PID LOG LOG0
    local pid="$1" log="$2" l0="$3" i summary
    printf '  waiting for it to attach and write its meta record (up to %ss; a first start on a kernel compiles for ~40s) ...' "$OPT_WAIT"
    for ((i = 0; i < OPT_WAIT; i++)); do
        if python3 "$DIR/lib/ebpfm_status.py" has-meta "$OUTPUT_DIR" "$HOST" "$pid" 2>/dev/null; then
            echo " ok"
            summary="$(_log_since "$log" "$l0" | grep -o 'features: [^)]*' | tail -n 1)"
            [ -n "$summary" ] && echo "  attached: ${summary#features: }"
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            echo " FAILED: it exited"
            _log_tail "$log" "$l0"
            rm -f "$PID_FILE" "$STATE_FILE"
            return 1
        fi
        sleep 1
    done
    echo " not yet after ${OPT_WAIT}s, though it runs (pid $pid)"
    _log_tail "$log" "$l0"
    echo "  $PROG status says when it records; $PROG stop stops it"
    return 4
}

# --- stop ----------------------------------------------------------------------
_node_stop() {
    local p u i
    if u="$(_node_active_unit)"; then
        if _systemctl_stop "$u"; then
            echo "node tier unit $u stopped on $HOST"
            echo "  note: the unit is still ENABLED; 'systemctl disable $u' to keep it down across reboots"
            rm -f "$NODE_PID_FILE"
            return 0
        fi
        echo "node tier runs as unit $u; stopping it takes root: sudo systemctl stop $u" >&2
        return 1
    fi
    if p="$(_node_running_pid)"; then
        kill "$p" 2>/dev/null || { echo "ebpfm: could not signal the node tier's loop (pid $p)" >&2; return 1; }
        for ((i = 0; i < 20; i++)); do _alive "$p" || break; sleep 0.25; done
        if _alive "$p"; then
            echo "node tier stopping on $HOST (pid $p): it exits once its current snapshot is written"
        else
            echo "node tier stopped on $HOST (pid $p)"
        fi
    else
        echo "node tier not running on $HOST"
    fi
    rm -f "$NODE_PID_FILE"
    return 0
}

_ebpf_stop() {
    need_root stop
    local u p i
    if u="$(_active_unit)"; then
        if _systemctl_stop "$u"; then
            echo "$NAME unit $u stopped on $HOST (SIGTERM -> truncated flush)"
            [ "$u" = "${NAME}.service" ] && \
                echo "  note: the persistent unit is still ENABLED; 'systemctl disable $NAME' to keep it down across reboots"
            rm -f "$PID_FILE" "$STATE_FILE"
            return 0
        fi
        echo "ebpfm: could not stop unit $u (systemctl status $u says why)" >&2
        return 1
    fi
    if p="$(_running_pid)"; then
        kill -TERM "$p" || { echo "ebpfm: could not signal pid $p" >&2; return 1; }
        printf '%s stopping on %s (pid %s; SIGTERM -> truncated flush) ...' "$NAME" "$HOST" "$p"
        for ((i = 0; i < 120; i++)); do _alive "$p" || break; sleep 0.5; done
        if _alive "$p"; then echo " still flushing after 60s: it exits once done"
        else echo " stopped"; fi
    else
        echo "$NAME not running on $HOST"
    fi
    rm -f "$PID_FILE" "$STATE_FILE"
    return 0
}

# As root, plainly; as a user, without polkit's password prompt in the middle.
_systemctl_stop() {
    if [ "$IS_ROOT" = 1 ]; then systemctl stop "$1" 2>/dev/null
    else systemctl --no-ask-password stop "$1" 2>/dev/null; fi
}

# The eBPF tier can be carried by either unit: the PERSISTENT one from
# install-unit (fleet deploys, survives reboot) or the TRANSIENT systemd-run
# service from `start` (one-offs). Echo whichever is active, so status, start and
# stop all agree -- otherwise `status` cheerfully reports NOT RUNNING while the
# installed unit is collecting.
_active_unit() {
    local u
    for u in "${NAME}.service" "${NAME}-${HOST}.service"; do
        systemctl is-active --quiet "$u" 2>/dev/null && { echo "$u"; return 0; }
    done
    return 1
}

# --- status --------------------------------------------------------------------
# What each tier is, for lib/ebpfm_status.py: `key=value` lines, `end` after each.
_node_facts() {
    local mode="" enabled="" restarts=""
    [ -n "$NI_STATE" ] && mode="$(_state_get "$NI_STATE" SNAPSHOT_MODE)"
    if [ -n "$NI_UNIT" ]; then
        enabled="$(systemctl is-enabled "$NI_UNIT" 2>/dev/null)"
        restarts="$(systemctl show -p NRestarts --value "$NI_UNIT" 2>/dev/null)"
    fi
    printf '%s\n' tier=node "running=$([ -n "$NI_HOW" ] && echo 1 || echo 0)" "how=$NI_HOW" \
        "unit=$NI_UNIT" "pid=$NI_PID" "enabled=$enabled" "restarts=$restarts" \
        "dir=$NODE_OUTPUT_DIR" "host=$HOST" "interval=$NODE_INTERVAL" "mode=$mode" \
        "log=$NODE_LOG" "state=$NI_STATE" "elapsed=$(_elapsed "$NI_PID")" \
        "note=$INSTANCE_NOTES_NODE" end
}
_ebpf_facts() {
    local enabled="" restarts=""
    if [ -n "$EI_UNIT" ]; then
        enabled="$(systemctl is-enabled "$EI_UNIT" 2>/dev/null)"
        restarts="$(systemctl show -p NRestarts --value "$EI_UNIT" 2>/dev/null)"
    fi
    printf '%s\n' tier=ebpf "running=$([ -n "$EI_HOW" ] && echo 1 || echo 0)" "how=$EI_HOW" \
        "unit=$EI_UNIT" "pid=$EI_PID" "enabled=$enabled" "restarts=$restarts" \
        "dir=$OUTPUT_DIR" "host=$HOST" "interval=$EBPF_INTERVAL" "log=$LOG" \
        "state=$EI_STATE" "elapsed=$(_elapsed "$EI_PID")" "note=$INSTANCE_NOTES_EBPF" end
}

cmd_status() {
    local args=(report --code-dir "$DIR" --prog "$PROG" --host "$HOST" --root "$IS_ROOT")
    [ "$OPT_JSON" = 1 ] && args+=(--json)
    _adopt_instances
    { if want_node; then _node_facts; fi; if want_ebpf; then _ebpf_facts; fi; } \
        | python3 "$DIR/lib/ebpfm_status.py" "${args[@]}"
}

# --- persistent systemd unit ------------------------------------------------
# `start` uses a TRANSIENT systemd-run service: correct for a one-off, but it does
# not survive a reboot and has no restart policy. For a fleet deployment the
# admin installs this instead, and systemd -- not run.sh, which is unprivileged
# and cannot sudo -- becomes the keepalive.
UNIT_DIR="${EBPFM_UNIT_DIR:-/etc/systemd/system}"
UNIT_SRC="$DIR/ebpfm.service"
UNIT_DST="$UNIT_DIR/${NAME}.service"
# The node tier's unit: the same loop `start` runs with nohup, kept by systemd.
NODE_UNIT_SRC="$DIR/ebpfm-node.service"
NODE_UNIT_DST="$UNIT_DIR/${NAME}-node.service"
# The ebpfm-collector package's own unit
PACKAGED_UNIT="${EBPFM_PACKAGED_UNIT:-/usr/lib/systemd/system/ebpfm.service}"

# The node tier's unit, when it is active: start, stop and status defer to it, so
# the loop never runs twice -- two loops would write every snapshot twice.
_node_active_unit() {
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl is-active --quiet "${NAME}-node.service" 2>/dev/null \
        && { echo "${NAME}-node.service"; return 0; }
    return 1
}

# A copy `ebpfm-bundle.sh` unpacked into a temporary folder: it is deleted after
# the command, and /tmp is emptied at boot, so units written from it would run
# nothing. A bundle leaves .ebpfm-bundle (its own path) in the folder it unpacks.
_refuse_temporary_copy() {   # COMMAND
    local from=""
    if [ -f "$DIR/.ebpfm-bundle" ]; then
        from="$(cat "$DIR/.ebpfm-bundle" 2>/dev/null)"
        printf 'ebpfm: %s: this copy was unpacked from %s into a temporary folder\n' "$1" "$from" >&2
        printf '  (%s), which goes when this command ends: units written from it would run\n' "$DIR" >&2
        printf '  nothing. Unpack it somewhere permanent, then run it from there:\n' >&2
        printf '    sudo %s --extract /opt/ebpfm && sudo /opt/ebpfm/ebpfm.sh %s\n' "$from" "$1" >&2
        exit 1
    fi
    case "$DIR" in
        /tmp/ebpfm.*|"${TMPDIR:-/tmp}"/ebpfm.*)
            printf 'ebpfm: %s: this copy runs from a temporary folder a bundle unpacked (%s),\n' "$1" "$DIR" >&2
            printf '  which /tmp loses at boot: units written from it would run nothing. Copy it\n' >&2
            printf '  somewhere permanent first, then run it from there:\n' >&2
            printf '    sudo mkdir -p /opt/ebpfm && sudo cp -a %s/. /opt/ebpfm/ && sudo /opt/ebpfm/ebpfm.sh %s\n' "$DIR" "$1" >&2
            exit 1 ;;
    esac
    return 0
}

# Where the ebpfm-collector package is installed, its unit and dependencies are
# in place already, and units from a copy would replace its unit.
_refuse_packaged() {   # COMMAND
    [ -e "$PACKAGED_UNIT" ] || return 0
    if [ ! -f "$UNIT_SRC" ]; then
        die "$1: this is the ebpfm-collector package's copy: the package installed ebpfm.service and its dependencies already. Run: systemctl enable --now ebpfm   (the package has no unit for the node tier yet: '$PROG start', as a user, runs that tier until a reboot)"
    fi
    die "$1: the ebpfm-collector package is installed ($PACKAGED_UNIT): units from this copy would replace its unit. Use the package's (systemctl enable --now ebpfm), or remove the package first"
}

cmd_install_unit() {
    _refuse_temporary_copy install-unit
    if [ "$OPT_DRY_RUN" = 1 ]; then
        _units_plan
        echo "nothing was changed (--dry-run)"
        return 0
    fi
    need_root install-unit
    _write_units || return 1
    echo
    echo "  systemctl enable --now $NAME     # the eBPF tier: start it and survive reboots"
    [ -f "$NODE_UNIT_SRC" ] && \
    echo "  systemctl enable --now $NAME-node   # the node tier, the same way"
    echo "  systemctl status $NAME"
}

_units_plan() {
    echo "install-unit would write:"
    echo "  $UNIT_DST: the eBPF tier, $DIR/ebpf_trace.py, writing to $OUTPUT_DIR"
    [ -f "$NODE_UNIT_SRC" ] && \
        echo "  $NODE_UNIT_DST: the node tier, every ${NODE_INTERVAL}s, writing to $NODE_OUTPUT_DIR"
    echo "  then: systemctl daemon-reload (enabling and starting nothing)"
}

# install-unit's work, which install shares
_write_units() {
    [ -f "$UNIT_SRC" ] || die "missing unit template: $UNIT_SRC"
    command -v systemctl >/dev/null 2>&1 || die "no systemctl on this host"
    if _running_pid >/dev/null 2>&1; then
        die "a transient ebpfm is running (pid $(_running_pid)); '$HINT stop' first"
    fi
    # A node loop `start` began would run beside the unit's once that is enabled, writing
    # every snapshot twice, and `stop` would then stop only the unit: refuse it the same way.
    if [ -f "$NODE_UNIT_SRC" ] && _node_running_pid >/dev/null 2>&1; then
        die "a node tier '$HINT start' began is running (pid $(_node_running_pid)); '$HINT stop' first"
    fi
    mkdir -p "$OUTPUT_DIR" "$UNIT_DIR"
    # The settings file the units read is the one this command read, and `install`
    # wrote: EBPFM_DEFAULT_FILE's, where it moves it off the templates' default. The
    # node daemon reads the file itself, so it is told the same; empty, neither reads one.
    local envf=() f
    if [ -z "$DEFAULT_FILE" ]; then
        envf=(-e 's#^EnvironmentFile=-/etc/default/ebpfm$#Environment=EBPFM_DEFAULT_FILE=#')
    elif [ "$DEFAULT_FILE" != /etc/default/ebpfm ]; then
        f="$DEFAULT_FILE"
        case "$f" in /*) ;; *) f="$(pwd -P)/$f" ;; esac     # a unit takes a full path only
        envf=(-e "s#^EnvironmentFile=-/etc/default/ebpfm\$#EnvironmentFile=-$f\nEnvironment=EBPFM_DEFAULT_FILE=$f#")
    fi
    sed -e "s#@EBPFM_DIR@#$DIR#g" -e "s#@EBPFM_OUTPUT_DIR@#$OUTPUT_DIR#g" ${envf[@]+"${envf[@]}"} \
        "$UNIT_SRC" > "$UNIT_DST" || die "cannot write $UNIT_DST"
    chmod 0644 "$UNIT_DST"
    # The node tier's unit, where this copy of the collector has its template: a
    # bundle or package from before it existed has none, and installs the one.
    local node_unit=0
    if [ -f "$NODE_UNIT_SRC" ]; then
        sed -e "s#@EBPFM_DIR@#$DIR#g" -e "s#@EBPFM_NODE_OUTPUT_DIR@#$NODE_OUTPUT_DIR#g" \
            ${envf[@]+"${envf[@]}"} "$NODE_UNIT_SRC" > "$NODE_UNIT_DST" \
            || die "cannot write $NODE_UNIT_DST"
        chmod 0644 "$NODE_UNIT_DST"
        node_unit=1
    fi
    systemctl daemon-reload || die "daemon-reload failed"
    echo "installed $UNIT_DST"
    echo "  collector: $DIR/ebpf_trace.py"
    echo "  output:    $OUTPUT_DIR/<date>/<host>.{snapshot,exits}.jsonl"
    if [ "$node_unit" = 1 ]; then
        echo "installed $NODE_UNIT_DST"
        echo "  node tier: $NODE_SNAPSHOT, every ${NODE_INTERVAL}s"
        echo "  output:    $NODE_OUTPUT_DIR/<date>/<host>.jsonl"
    fi
    return 0
}

cmd_uninstall_unit() {
    local u any=0
    if [ "$OPT_DRY_RUN" = 1 ]; then
        for u in "$UNIT_DST" "$NODE_UNIT_DST"; do
            [ -f "$u" ] || continue
            any=1
            echo "would stop and disable $(basename "$u"), and remove $u"
        done
        if [ "$any" = 1 ]; then echo "then: systemctl daemon-reload"; else echo "no unit installed in $UNIT_DIR"; fi
        echo "nothing was changed (--dry-run)"
        return 0
    fi
    need_root uninstall-unit
    for u in "$UNIT_DST" "$NODE_UNIT_DST"; do
        [ -f "$u" ] || continue
        any=1
        systemctl disable --now "$(basename "$u")" 2>/dev/null
        rm -f "$u"
        echo "removed $u"
    done
    [ "$any" = 1 ] || { echo "no unit installed in $UNIT_DIR"; return 0; }
    systemctl daemon-reload 2>/dev/null
    return 0
}

# --- install -------------------------------------------------------------------
# KEY=VALUE made active in the settings file, the way the deploy installer writes
# it: every active assignment of KEY goes, the first commented `#KEY=` line
# becomes the new one (else it is appended), and every other line stays. A file
# that does not exist is made from the commented defaults first.
_settings_file_write() {   # FILE KEY VALUE [KEY VALUE ...]
    local f="$1" k v body
    shift
    if [ ! -e "$f" ]; then
        mkdir -p "$(dirname "$f")" || return 1
        _settings_py defaults-file > "$f" || return 1
        chmod 0644 "$f"
        echo "  created $f from the commented defaults"
    fi
    while [ $# -ge 2 ]; do
        k="$1"; v="$2"; shift 2
        if [ "${_FILE_VALS[$k]+set}" = set ] && [ "${_FILE_VALS[$k]}" = "$v" ]; then
            echo "  $k=$v (already set)"
            continue
        fi
        body="$(K="$k" V="$v" awk '
            BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"]; done = 0; re = "^[ \t]*" k "[ \t]*=" }
            $0 ~ re { next }
            !done && index($0, "#" k "=") == 1 { print k "=" v; done = 1; next }
            { print }
            END { if (!done) print k "=" v }' "$f")" || return 1
        printf '%s\n' "$body" > "$f" || return 1
        _FILE_VALS[$k]="$v"
        echo "  $k=$v"
    done
    return 0
}

# Where the eBPF tier fails its check, install keeps the node tier's four
# per-process sections with SNAPSHOT_MODE=full, marked as its own; an install
# that finds the eBPF tier ready takes out that line, and no other.
_INSTALL_MARK='# `ebpfm install` set the next line: the eBPF tier was not ready, so the node tier keeps every section'
_mark_full() {   # FILE
    local body
    _settings_file_write "$1" SNAPSHOT_MODE full || return 1
    grep -qxF "$_INSTALL_MARK" "$1" && return 0
    body="$(M="$_INSTALL_MARK" awk '
        BEGIN { m = ENVIRON["M"] }
        /^SNAPSHOT_MODE=full$/ && !done { print m; done = 1 }
        { print }' "$1")" || return 1
    printf '%s\n' "$body" > "$1"
}
_unmark_full() {   # FILE: 0 when it took out the line install set
    local body
    grep -qxF "$_INSTALL_MARK" "$1" 2>/dev/null || return 1
    body="$(M="$_INSTALL_MARK" awk '
        BEGIN { m = ENVIRON["M"] }
        $0 == m { skip = 1; next }
        skip && /^SNAPSHOT_MODE=full$/ { skip = 0; next }
        { skip = 0; print }' "$1")" || return 1
    printf '%s\n' "$body" > "$1"
    unset '_FILE_VALS[SNAPSHOT_MODE]'
    unset SNAPSHOT_MODE
    return 0
}

cmd_install() {
    _refuse_temporary_copy install
    _refuse_packaged install
    local out="$OUTPUT_DIR" node="$NODE_OUTPUT_DIR" k e c cur sys_plan="" ck_node ck_ebpf
    local rc_e=0 rc_n=0 started_e=0 started_n=0 pid t0 f0 s0 verdict_rc
    if [ "$IS_ROOT" != 1 ]; then
        [ "$OPT_DRY_RUN" = 1 ] || need_root install
        # As a user, the plan shows the folders root would use.
        _set_by_user EBPFM_OUTPUT_DIR || { out=/var/log/ebpfm; [ -d /scratch ] && out=/scratch/ebpfm; }
        _set_by_user EBPFM_NODE_OUTPUT_DIR || { node=/var/log/ebpfm-login; [ -d /scratch ] && node=/scratch/ebpfm-login; }
    fi
    # The environment and the settings file must not name two folders for one tier:
    # the units would write to one, and this shell look in the other.
    for k in EBPFM_OUTPUT_DIR EBPFM_NODE_OUTPUT_DIR; do
        case " $_ENV_NAMES " in *" $k "*) ;; *) continue ;; esac
        if [ "$k" = EBPFM_OUTPUT_DIR ]; then e="$out"; else e="$node"; fi
        if [ "${_FILE_VALS[$k]+set}" = set ] && [ "${_FILE_VALS[$k]}" != "$e" ]; then
            die "install: $DEFAULT_FILE sets $k=${_FILE_VALS[$k]}, and the environment $k=$e: settle on one"
        fi
    done
    _bootstrap_plan
    for c in kernel.sched_schedstats kernel.task_delayacct; do
        cur="$(_sysctl_get "$c")"
        case "$cur" in
            1) ;;
            missing) sys_plan="$sys_plan$c: no such knob here (compile-time only); " ;;
            unreadable) sys_plan="$sys_plan$c -> 1 (unreadable as this user); " ;;
            *) sys_plan="$sys_plan$c $cur -> 1; " ;;
        esac
    done

    echo "== ebpfm install on $HOST (collector $(_version))$([ "$OPT_DRY_RUN" = 1 ] && echo ' -- the plan; nothing is changed (--dry-run)')"
    [ "$IS_ROOT" = 1 ] || echo "   (not root: the real run needs sudo; the folders below are root's)"
    if [ -z "$BOOT_MGR" ]; then
        echo "1. dependencies   none installed: no apt-get, dnf or yum here (python3-bcc and the kernel's headers by hand)"
    elif [ "${#BOOT_PKGS[@]}" -gt 0 ]; then
        echo "1. dependencies   install with $BOOT_MGR: ${BOOT_PKGS[*]}"
    else
        echo "1. dependencies   all present"
    fi
    for c in "${BOOT_SOURCES[@]+"${BOOT_SOURCES[@]}"}"; do
        echo "                  add the universe component to $c"
    done
    echo "   sysctls        ${sys_plan:-both already 1; }kept in $SYSCTL_CONF"
    if [ -e "$DEFAULT_FILE" ]; then
        echo "2. settings       $DEFAULT_FILE: kept, with"
    else
        echo "2. settings       $DEFAULT_FILE: made from the commented defaults, with"
    fi
    echo "                  EBPFM_OUTPUT_DIR=$out"
    echo "                  EBPFM_NODE_OUTPUT_DIR=$node"
    echo "3. units          $UNIT_DST, $NODE_UNIT_DST; systemctl daemon-reload"
    echo "4. start          check; each tier that passes: systemctl enable --now, then its first record$([ "$OPT_NO_WAIT" = 1 ] && echo ' (not waited for: --no-wait)')"
    echo "5. verdict        READY: both tiers, READY: node tier only, or NOT READY: N FAIL, with fixes"
    if [ "$OPT_DRY_RUN" = 1 ]; then
        echo "nothing was changed (--dry-run)"
        return 0
    fi

    echo
    echo "== 1. dependencies and kernel settings"
    ( _bootstrap_apply ) || echo "  dependencies: not all installed (above); the check below says what that costs"
    _set_sysctls
    _forget_features
    echo "== 2. settings: $DEFAULT_FILE"
    _settings_file_write "$DEFAULT_FILE" EBPFM_OUTPUT_DIR "$out" EBPFM_NODE_OUTPUT_DIR "$node" \
        || die "install: cannot write $DEFAULT_FILE"
    OUTPUT_DIR="$out"; NODE_OUTPUT_DIR="$node"
    echo "== 3. units"
    _write_units || return 1
    echo "== 4. check"
    CK_NO_VERDICT=1
    cmd_check
    CK_NO_VERDICT=0
    ck_node="${#CK_FAILS_NODE[@]}"; ck_ebpf="${#CK_FAILS_EBPF[@]}"
    echo "== 5. start"
    if [ "$ck_node" = 0 ] && [ "$ck_ebpf" = 0 ]; then
        if _unmark_full "$DEFAULT_FILE"; then
            echo "settings: took out SNAPSHOT_MODE=full, which an install set while the eBPF tier was not ready"
            if systemctl is-active --quiet "${NAME}-node.service" 2>/dev/null \
                    && systemctl restart "${NAME}-node.service"; then
                echo "node tier: restarted, to leave the four per-process sections to the eBPF tier"
            fi
        fi
        t0="$(date +%s)"
        if systemctl is-active --quiet "${NAME}.service" 2>/dev/null; then
            # an install run again (an upgrade): the tier keeps running, on its old code
            started_e=1
            systemctl enable "${NAME}.service" >/dev/null 2>&1
            echo "ebpf tier: ebpfm.service already runs; it runs the installed code once restarted"
            echo "  (systemctl restart ebpfm, when you choose: it writes its live processes as \`truncated\`)"
        elif systemctl enable --now "${NAME}.service"; then
            started_e=1
            echo "ebpf tier: ebpfm.service enabled and started"
            pid="$(systemctl show -p MainPID --value "${NAME}.service" 2>/dev/null)"
            if [ "$OPT_NO_WAIT" = 1 ]; then :
            elif [ -n "$pid" ] && [ "$pid" != 0 ]; then _await_ebpf "$pid" "journal:${NAME}.service" "$t0"; rc_e=$?
            else echo "  FAILED: ebpfm.service exited at once"; _log_tail "journal:${NAME}.service" "$t0"; rc_e=1; fi
        else
            rc_e=1
        fi
        [ "$rc_e" = 1 ] && CK_FAILS_EBPF+=("ebpf tier${_US}did not start: the log above says why${_US}fix it, then: systemctl restart ebpfm")
    else
        echo "ebpf tier: not started -- its check FAILs below; once fixed: systemctl enable --now ebpfm"
        if [ "$ck_node" = 0 ] && [ -z "${SNAPSHOT_MODE:-}" ]; then
            # without the eBPF tier, the node tier must keep the four per-process sections
            _mark_full "$DEFAULT_FILE"
            echo "  so the node tier keeps every section: SNAPSHOT_MODE=full in $DEFAULT_FILE (an install that finds the eBPF tier ready takes it out)"
        fi
    fi
    if [ "$ck_node" = 0 ]; then
        f0="$NODE_OUTPUT_DIR/$(date +%F)/$HOST.jsonl"; s0="$(_size "$f0")"; t0="$(date +%s)"
        if systemctl is-active --quiet "${NAME}-node.service" 2>/dev/null; then
            started_n=1
            systemctl enable "${NAME}-node.service" >/dev/null 2>&1
            echo "node tier: ebpfm-node.service already runs; it runs the installed code once restarted"
            echo "  (systemctl restart ebpfm-node: it loses at most the snapshot it is taking)"
        elif systemctl enable --now "${NAME}-node.service"; then
            started_n=1
            echo "node tier: ebpfm-node.service enabled and started"
            pid="$(systemctl show -p MainPID --value "${NAME}-node.service" 2>/dev/null)"
            if [ "$OPT_NO_WAIT" = 1 ]; then :
            elif [ -n "$pid" ] && [ "$pid" != 0 ]; then _await_node "$pid" "$f0" "$s0" "journal:${NAME}-node.service" "$t0"; rc_n=$?
            else echo "  FAILED: ebpfm-node.service exited at once"; _log_tail "journal:${NAME}-node.service" "$t0"; rc_n=1; fi
        else
            rc_n=1
        fi
        [ "$rc_n" = 1 ] && CK_FAILS_NODE+=("node tier${_US}did not start: the log above says why${_US}fix it, then: systemctl restart ebpfm-node")
    else
        echo "node tier: not started -- its check FAILs below"
    fi
    echo "== verdict"
    _verdict; verdict_rc=$?
    [ "$started_e" = 1 ] || [ "$started_n" = 1 ] && echo "  $PROG status shows each tier; the units start again at boot"
    if [ "$verdict_rc" != 0 ]; then return 1; fi
    if [ "$rc_e" = 4 ] || [ "$rc_n" = 4 ]; then return 4; fi
    return 0
}

# --- config and version --------------------------------------------------------
cmd_config() {
    local extra=()
    if [ "$OPT_DEFAULTS_FILE" = 1 ]; then _settings_py defaults-file; return; fi
    [ "$OPT_JSON" = 1 ] && extra+=(--json)
    _settings_py config "${SETTINGS_ARGS[@]}" "${extra[@]+"${extra[@]}"}" "${ARGS[@]+"${ARGS[@]}"}"
}

cmd_version() { printf 'ebpfm %s\n' "$(_version)"; }

# --- tier dispatchers ------------------------------------------------------
# The eBPF tier first: if it does not come up, the node tier keeps the four
# per-process sections it would otherwise leave to it. Exit status: 0 every tier
# runs and is confirmed (or already ran); 1 a tier failed; 4 one is not yet
# confirmed.
cmd_start() {
    local rc_e=0 rc_n=0 mode=""
    if want_ebpf; then _ebpf_start; rc_e=$?; fi
    if want_node; then
        if want_ebpf && [ "$rc_e" = 1 ] && [ -z "${SNAPSHOT_MODE:-}" ]; then
            mode=full
            echo "node tier: the eBPF tier did not start, so the node tier keeps every section (mode full)"
        fi
        _node_start $mode; rc_n=$?
    fi
    if [ "$rc_e" = 1 ] || [ "$rc_n" = 1 ]; then return 1; fi
    if [ "$rc_e" = 4 ] || [ "$rc_n" = 4 ]; then return 4; fi
    return 0
}

cmd_stop() {
    # Stop the eBPF tier FIRST: SIGTERM makes it flush every live tracked process
    # as event="truncated", and those records are worth more with the node tier
    # still sampling alongside them.
    local rc=0
    if want_ebpf; then _ebpf_stop || rc=1; fi
    if want_node; then _node_stop || rc=1; fi
    return "$rc"
}

# `tail [n] [stream]` -- both eBPF streams by default, since which one carries
# the record you are looking for is exactly what you may not know yet; the node
# tier's file for a user who runs only that tier.
cmd_tail() {
    local n="${1:-3}" want="${2:-}" s f any=0 tried=""
    for s in ${want:-$EBPF_STREAMS}; do
        case "$s" in
            node) f="$NODE_OUTPUT_DIR/$(date +%F)/$HOST.jsonl" ;;
            *)    f="$(_ebpf_file "$s")" ;;
        esac
        tried="$tried $f"
        [ -f "$f" ] || continue
        any=1
        echo "== $s ($f)"
        if command -v jq >/dev/null 2>&1; then tail -n "$n" "$f" | jq .; else tail -n "$n" "$f"; fi
    done
    [ "$any" = 1 ] || { echo "no records yet today:$tried"; return 1; }
}
_default_streams() { if want_ebpf; then echo "$EBPF_STREAMS"; else echo node; fi; }

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

    # `recs` is the whole capture, so it sums both streams. The perf counters
    # come off the `stop` record, which rides the SNAPSHOT stream: the last one
    # there -- the last line of the exits file would silently be an exit record
    # and report every counter as null.
    local d; d="$tmp/$(date +%F)"
    local f s n recs=0 stop lostline seenline seentot bytesline
    for s in $EBPF_STREAMS; do
        f="$(_jsonl_file "$d" "$HOST.$s")" || continue
        n="$(_feed_count "$f")"
        case "$n" in ''|*[!0-9]*) n=0 ;; esac
        recs=$((recs + n))
    done
    f="$(_jsonl_file "$d" "$HOST.snapshot")" || f=""
    stop="$( [ -n "$f" ] && _last_stop "$f" )"
    lostline="$(printf '%s' "$stop" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(json.dumps(d.get("perf_lost")))' 2>/dev/null || echo '{}')"
    seenline="$(printf '%s' "$stop" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(json.dumps(d.get("perf_seen")))' 2>/dev/null || echo '{}')"
    seentot="$(printf '%s' "$stop" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(sum((d.get("perf_seen") or {}).values()))' 2>/dev/null || echo 0)"
    # From stop.output: the bytes and write(2)s up to the stop record.
    bytesline="$(printf '%s' "$stop" | python3 -c '
import json,sys
o=(json.load(sys.stdin).get("output") or {}).get("jsonl")
print("jsonl %.2f MB in %d writes" % (o["bytes"]/1e6, o["writes"]) if o else "n/a")' 2>/dev/null || echo 'n/a')"

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
# Packs the runtime files, the docs and a VERSION stamp (this copy's version).
# The file it writes runs a command from a temporary copy and deletes it after
# (`start` keeps it: the tiers run from it), or unpacks itself for good with
# --extract DIR. ebpfm.sh refuses to install units from the temporary copy.
cmd_bundle() {
    local out="${1:-ebpfm-bundle.sh}" base stage v f
    base="$(basename "$DIR")"
    v="$(_version)"
    stage="$(mktemp -d)" || die "cannot make a staging folder"
    mkdir -p "$stage/$base/lib"
    for f in ebpfm.sh ebpf_trace.py node_snapshot.py; do
        cp -p "$DIR/$f" "$stage/$base/$f" || { rm -rf "$stage"; die "bundle: $DIR/$f is missing"; }
    done
    cp -p "$DIR"/lib/*.py "$stage/$base/lib/" || { rm -rf "$stage"; die "bundle: $DIR/lib is missing"; }
    for f in ebpfm.service ebpfm-node.service README.md OPERATIONS.md WHAT-IT-COLLECTS.md COVERAGE.md \
             VENDORED.md .vendored.sha256; do
        [ -f "$DIR/$f" ] && cp -p "$DIR/$f" "$stage/$base/$f"
    done
    printf '%s\n' "$v" > "$stage/$base/VERSION"
    {
        cat <<'HDR'
#!/usr/bin/env bash
# Self-extracting ebpfm collector. Runs ebpfm.sh with whatever arguments you pass,
# from a temporary copy it deletes afterwards (`start` keeps it: the tiers run
# from it):
#     sudo ./ebpfm-bundle.sh check
# To install it, unpack it somewhere permanent first, and run it from there:
#     sudo ./ebpfm-bundle.sh --extract /opt/ebpfm && sudo /opt/ebpfm/ebpfm.sh install
set -euo pipefail
# Resolve our own path: $0 is whatever the caller typed, and `bash bundle.sh`
# gives a bare relative name that fails once anything changes directory.
SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"
payload() { sed -e '1,/^__EBPFM_PAYLOAD__$/d' "$SELF" | base64 -d; }
if [ "${1:-}" = --extract ]; then
    [ -n "${2:-}" ] || { echo "usage: $0 --extract DIR" >&2; exit 2; }
    mkdir -p "$2"
    payload | tar xz -C "$2" --strip-components=1
    echo "unpacked the collector into $2: run $2/ebpfm.sh help" >&2
    exit 0
fi
D="$(mktemp -d "${TMPDIR:-/tmp}/ebpfm.XXXXXX")"
payload | tar xz -C "$D"
C="$(echo "$D"/*/ebpfm.sh)"; C="${C%/ebpfm.sh}"
printf '%s\n' "$SELF" > "$C/.ebpfm-bundle"
rc=0
EBPFM_PROG="$0" EBPFM_SELF="$SELF" bash "$C/ebpfm.sh" "$@" || rc=$?
case "${1:-}" in
    start) echo "kept $C: the tiers just started run from it (use --extract for a lasting copy)" >&2 ;;
    *) rm -rf "$D" ;;
esac
exit "$rc"
__EBPFM_PAYLOAD__
HDR
        tar czf - -C "$stage" "$base" | base64
    } > "$out"
    rm -rf "$stage"
    chmod +x "$out"
    case "$out" in /*) echo "wrote $out ($(du -h "$out" | cut -f1), collector $v) — run: sudo $out check" ;;
                     *)  echo "wrote $out ($(du -h "$out" | cut -f1), collector $v) — run: sudo ./$out check" ;; esac
}

# ---------------------------------------------------------------------------
_main() {   # COMMAND ARGS...
    local cmd="$1"
    shift
    _spec "$cmd" >/dev/null || _unknown_command "$cmd"
    _parse_args "$cmd" "$@"
    case "$cmd" in
        help)           cmd_help "${ARGS[@]+"${ARGS[@]}"}" ;;
        version)        cmd_version ;;
        config)         cmd_config ;;
        check)          cmd_check ;;
        bootstrap)      cmd_bootstrap ;;
        install)        _require_valid_settings install; cmd_install ;;
        install-unit)   _require_valid_settings install-unit; cmd_install_unit ;;
        uninstall-unit) cmd_uninstall_unit ;;
        features)       _require_valid_settings features; cmd_features ;;
        dump-c)         _require_valid_settings dump-c; cmd_dump_c ;;
        once)           _require_valid_settings once; cmd_once "${ARGS[@]+"${ARGS[@]}"}" ;;
        start)          _require_valid_settings start; cmd_start ;;
        stop)           cmd_stop ;;
        status)         cmd_status ;;
        tail)           _adopt_instances; cmd_tail "${ARGS[0]:-3}" "${ARGS[1]:-$(_default_streams)}" ;;
        overhead)       _require_valid_settings overhead; cmd_overhead ;;
        bundle)         cmd_bundle "${ARGS[@]+"${ARGS[@]}"}" ;;
    esac
}

VERB="${1:-help}"
case "$VERB" in -h|--help) VERB=help ;; -V|--version) VERB=version ;; esac
case "$VERB" in
    _node_daemon) shift; _node_loop ;;   # internal: the node tier's loop (start, and ebpfm-node.service)
    *) [ $# -gt 0 ] && shift; _main "$VERB" "$@" ;;
esac
