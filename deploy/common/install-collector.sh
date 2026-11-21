#!/bin/bash
# install-collector.sh [--no-bootstrap] [--no-check] [--restart]: run the collector on this host under
# systemd, both tiers, where deploy/single's or deploy/fleet's dashboard reads them. Run as
# root, from a checkout of this repository: the units run the collector from it.
#
#   1. The group COLLECTOR_GROUP (made if missing) gets to read what the collector writes,
#      and no one else: EBPFM_GROUP in /etc/default/ebpfm, which is made from the RPM's
#      defaults if there is none (packaging/rpm/ebpfm.default; EBPFM_MIN_UID=0 among them).
#   2. ebpfm.sh bootstrap: the collector's dependencies and its two sysctls. Then check,
#      which must report no FAIL before anything starts.
#   3. ebpfm.sh install-unit: ebpfm.service (the eBPF tier) and ebpfm-node.service (the node
#      tier), writing to EBPF_DIR and NODE_DIR. Both are enabled and started.
#
# From the environment: EBPF_DIR (/var/log/ebpfm), NODE_DIR (/var/log/ebpfm-login), and
# COLLECTOR_GROUP (ebpfm; a group /etc/default/ebpfm already names is kept). An output folder
# /etc/default/ebpfm already names is where the collector writes, and wins
# (collector-paths.sh); one named differently there and here is refused. Run it again
# after a `git pull`: the units are written again, and a running collector keeps running --
# a restart flushes every live process as `truncated` -- so restart it when you choose:
#   systemctl restart ebpfm ebpfm-node
# Except one started without EBPFM_GROUP=<the group>, which reads it only at start and would
# give every file it makes from now on to root alone: that is refused, unless --restart lets
# this restart it.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
EBPFM=${EBPFM_SH:-$repo/collector/ebpfm.sh}
DEFAULTS=${EBPFM_DEFAULT_FILE:-/etc/default/ebpfm}
PACKAGED=${EBPFM_PACKAGED_UNIT:-/usr/lib/systemd/system/ebpfm.service}

say() { printf '==> %s\n' "$*"; }
die() { printf 'install-collector.sh: %s\n' "$*" >&2; exit 1; }
# shellcheck source=SCRIPTDIR/collector-paths.sh
. "$here/collector-paths.sh"

bootstrap=1 check=1 restart=0
for a in "$@"; do
  case $a in
    --no-bootstrap) bootstrap=0 ;;
    --no-check) check=0 ;;
    --restart) restart=1 ;;
    *) echo "usage: install-collector.sh [--no-bootstrap] [--no-check] [--restart]" >&2; exit 2 ;;
  esac
done

# UNIT runs, and its process was started without EBPFM_GROUP=$GROUP in its environment: both
# tiers read it once, at start (ebpf_trace.py's OUTPUT_GID; ebpfm.sh's export), so whatever
# the defaults file says now, that process gives its new files to root alone.
_runs_without_group() {
  local pid
  systemctl is-active --quiet "$1" 2>/dev/null || return 1
  pid=$(systemctl show -p MainPID --value "$1" 2>/dev/null || true)
  case $pid in ''|0|*[!0-9]*) return 1 ;; esac
  if tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -qx "EBPFM_GROUP=$GROUP"; then
    return 1
  fi
  return 0
}

[ "$(id -u)" = 0 ] || die "run as root: the eBPF tier loads programs into the kernel"
command -v systemctl >/dev/null 2>&1 || die "no systemctl: the collector runs as systemd units"
[ -f "$EBPFM" ] || die "no collector at $EBPFM"
if [ -e "$PACKAGED" ]; then
  die "the ebpfm-collector package is installed ($PACKAGED). This would run a second collector
  from this checkout beside it, and every record would be written twice. Use the package's
  unit instead (deploy/README.md, \"The collector from the RPM\"), or remove the package."
fi

# 1. Who may read the output. A group /etc/default/ebpfm already names is kept: the
#    collector has been writing for it.
if [ ! -e "$DEFAULTS" ]; then
  say "$DEFAULTS, from packaging/rpm/ebpfm.default"
  install -m 644 "$repo/packaging/rpm/ebpfm.default" "$DEFAULTS"
fi
# Where the collector writes: a folder $DEFAULTS names wins over the units' own.
collector_paths
for d in "$EBPF_DIR" "$NODE_DIR"; do
  case $d in /?*) ;; *) die "EBPF_DIR and NODE_DIR must be absolute paths (got '$d')" ;; esac
done
named=$(_ebpfm_default EBPFM_GROUP)      # without the quotes systemd takes off
GROUP=${COLLECTOR_GROUP:-${named:-ebpfm}}
if [ -n "$named" ] && [ "$named" != "$GROUP" ]; then
  die "$DEFAULTS gives the output to group $named, and COLLECTOR_GROUP says $GROUP; settle on one"
fi
getent group "$GROUP" >/dev/null || { say "group $GROUP"; groupadd --system "$GROUP"; }
if [ -z "$named" ]; then
  say "EBPFM_GROUP=$GROUP in $DEFAULTS"
  # An empty assignment leaves the group as it is, and systemd keeps the last one: none may
  # stay to outlast the group set here.
  sed -i '/^EBPFM_GROUP=/d' "$DEFAULTS"
  if grep -q '^#EBPFM_GROUP=' "$DEFAULTS"; then
    sed -i "0,/^#EBPFM_GROUP=.*/s//EBPFM_GROUP=$GROUP/" "$DEFAULTS"
  else
    printf 'EBPFM_GROUP=%s\n' "$GROUP" >> "$DEFAULTS"
  fi
fi

# 2. Dependencies and sysctls, then the collector's own check.
if [ "$bootstrap" = 1 ]; then
  say "ebpfm.sh bootstrap"
  if ! bash "$EBPFM" bootstrap && [ "$check" = 1 ]; then
    die "bootstrap ended with a FAIL above; fix it and run this again (--no-check starts the collector anyway)"
  fi
elif [ "$check" = 1 ]; then
  say "ebpfm.sh check"
  bash "$EBPFM" check || die "check reported a FAIL above; fix it and run this again"
fi

# 3. The units. Output written before EBPFM_GROUP was set keeps its group, so the folders
#    are given to the group here: every day, sealed or live, readable by it alone.
say "units, writing to $EBPF_DIR and $NODE_DIR"
EBPFM_OUTPUT_DIR=$EBPF_DIR EBPFM_NODE_OUTPUT_DIR=$NODE_DIR bash "$EBPFM" install-unit
for d in "$EBPF_DIR" "$NODE_DIR"; do
  mkdir -p "$d"
  chgrp -R "$GROUP" "$d"
  chmod -R g+rX,g-w,o-rwx "$d"
done
# A tier already running without the group keeps making files only root can read: it is
# restarted with --restart, and refused otherwise, rather than left so and called readable.
stale=()
for u in ebpfm.service ebpfm-node.service; do
  if _runs_without_group "$u"; then stale+=("$u"); fi
done
if [ ${#stale[@]} -gt 0 ]; then
  if [ "$restart" = 0 ]; then
    die "${stale[*]} started without EBPFM_GROUP=$GROUP and read it only at start: every file
  they make from now on would be readable by root alone, the dashboard's included. Run this
  again with --restart to restart them (the eBPF tier then flushes every live process as
  \`truncated\`), or restart them yourself and run it again."
  fi
  say "restarting ${stale[*]}, started without EBPFM_GROUP=$GROUP (the eBPF tier flushes its live processes as \`truncated\`)"
  systemctl restart "${stale[@]}"
fi
systemctl enable --now ebpfm.service ebpfm-node.service
say "the collector runs; its output is readable by group $GROUP"
bash "$EBPFM" status || true
