#!/bin/bash
# install-collector.sh [--no-bootstrap] [--no-check] [--restart] [--plan] [--help]: run the
# collector on this host under systemd, both tiers, where deploy/single's or deploy/fleet's
# dashboard reads them. Run as root, from a checkout of this repository: the units run
# the collector from it.
#
#   1. The group COLLECTOR_GROUP (made if missing) gets to read what the collector writes,
#      and no one else: EBPFM_GROUP in /etc/default/ebpfm, which is made from the RPM's
#      defaults if there is none (packaging/rpm/ebpfm.default; EBPFM_MIN_UID=0 among them).
#      The output folders go there too (EBPFM_OUTPUT_DIR, EBPFM_NODE_OUTPUT_DIR), so a
#      later run that is not told EBPF_DIR again, and `ebpfm status`, find them.
#   2. ebpfm.sh bootstrap: the collector's dependencies and its two sysctls. Then check,
#      which must report no FAIL before anything starts.
#   3. ebpfm.sh install-unit: ebpfm.service (the eBPF tier) and ebpfm-node.service (the node
#      tier), writing to EBPF_DIR and NODE_DIR. Both are enabled and started.
#
# --plan prints what it would do and changes nothing (--help: this text).
#
# From the environment: EBPF_DIR (/var/log/ebpfm), NODE_DIR (/var/log/ebpfm-login), and
# COLLECTOR_GROUP (ebpfm; a group /etc/default/ebpfm already names is kept). An output folder
# /etc/default/ebpfm already names is where the collector writes, and wins
# (collector-paths.sh); one named differently there and here is refused. What it made
# (the defaults file, the group, the sysctls) is noted in /etc/rc-dashboard/install.env,
# for deploy/common/uninstall.sh. Run it again after a `git pull`: the units are written
# again, and a running collector keeps running -- a restart flushes every live process as
# `truncated` -- so restart it when you choose:
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
# shellcheck source=SCRIPTDIR/lib.sh
. "$here/lib.sh"
# shellcheck source=SCRIPTDIR/collector-paths.sh
. "$here/collector-paths.sh"

bootstrap=1 check=1 restart=0
for a in "$@"; do
  case $a in
    --no-bootstrap) bootstrap=0 ;;
    --no-check) check=0 ;;
    --restart) restart=1 ;;
    --plan) PLAN=1 ;;
    -h|--help) show_help "$0"; exit 0 ;;
    *) echo "usage: install-collector.sh [--no-bootstrap] [--no-check] [--restart] [--plan] [--help]" >&2; exit 2 ;;
  esac
done
[ "$PLAN" = 1 ] && say "the plan: nothing below is done"

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

if [ "$(id -u)" != 0 ]; then
  [ "$PLAN" = 1 ] || die "run as root: the eBPF tier loads programs into the kernel"
  say "not root: files only root may read are taken as missing, so the plan may differ"
fi
command -v systemctl >/dev/null 2>&1 || die "no systemctl: the collector runs as systemd units"
[ -f "$EBPFM" ] || die "no collector at $EBPFM"
if [ -e "$PACKAGED" ]; then
  die "the ebpfm-collector package is installed ($PACKAGED). This would run a second collector
  from this checkout beside it, and every record would be written twice. Use the package's
  unit instead (deploy/README.md, \"The collector from the RPM\"), or remove the package."
fi
install_env_load MADE_DEFAULTS MADE_GROUP RAN_BOOTSTRAP
MADE_DEFAULTS=${MADE_DEFAULTS:-no} MADE_GROUP=${MADE_GROUP:-no} RAN_BOOTSTRAP=${RAN_BOOTSTRAP:-no}

# 1. Who may read the output. A group /etc/default/ebpfm already names is kept: the
#    collector has been writing for it.
if [ ! -e "$DEFAULTS" ]; then
  say "$DEFAULTS, from packaging/rpm/ebpfm.default"
  run install -m 644 "$repo/packaging/rpm/ebpfm.default" "$DEFAULTS"
  MADE_DEFAULTS=yes
  if [ "$PLAN" = 1 ]; then          # read it as it will be, and name it where it will be
    DEFAULTS_SHOWN=$DEFAULTS DEFAULTS=$repo/packaging/rpm/ebpfm.default
  fi
fi
# Where the collector writes: a folder $DEFAULTS names wins over the units' own.
FROM_EBPF_DIR=env FROM_NODE_DIR=env
collector_paths
for d in "$EBPF_DIR" "$NODE_DIR"; do
  [[ $d =~ ^/[A-Za-z0-9._/@+-]*$ ]] || die "EBPF_DIR and NODE_DIR must be absolute paths, with no spaces or quotes (got '$d')"
done
named=$(_ebpfm_default EBPFM_GROUP)      # without the quotes systemd takes off
GROUP=${COLLECTOR_GROUP:-${named:-ebpfm}}
if [ -n "$named" ] && [ "$named" != "$GROUP" ]; then
  die "$DEFAULTS gives the output to group $named, and COLLECTOR_GROUP says $GROUP; settle on one"
fi
if ! getent group "$GROUP" >/dev/null; then
  say "group $GROUP"
  run groupadd --system "$GROUP"
  MADE_GROUP=yes
fi
[ -n "$named" ] || set_default EBPFM_GROUP "$GROUP"
set_default EBPFM_OUTPUT_DIR "$EBPF_DIR"
set_default EBPFM_NODE_OUTPUT_DIR "$NODE_DIR"

# 2. Dependencies and sysctls, then the collector's own check.
if [ "$bootstrap" = 1 ]; then
  say "ebpfm.sh bootstrap"
  if [ "$PLAN" = 1 ]; then
    run bash "$EBPFM" bootstrap
  elif ! bash "$EBPFM" bootstrap && [ "$check" = 1 ]; then
    die "bootstrap ended with a FAIL above; fix it and run this again (--no-check starts the collector anyway)"
  fi
  RAN_BOOTSTRAP=yes
elif [ "$check" = 1 ]; then
  say "ebpfm.sh check"
  if [ "$PLAN" = 1 ]; then
    bash "$EBPFM" check || say "check reports a FAIL above: a run would stop here"
  else
    bash "$EBPFM" check || die "check reported a FAIL above; fix it and run this again"
  fi
fi

# 3. The units. Output written before EBPFM_GROUP was set keeps its group, so the folders
#    are given to the group here: every day, sealed or live, readable by it alone.
say "units, writing to $EBPF_DIR and $NODE_DIR"
run env EBPFM_OUTPUT_DIR="$EBPF_DIR" EBPFM_NODE_OUTPUT_DIR="$NODE_DIR" bash "$EBPFM" install-unit
for d in "$EBPF_DIR" "$NODE_DIR"; do
  run mkdir -p "$d"
  run chgrp -R "$GROUP" "$d"
  run chmod -R g+rX,g-w,o-rwx "$d"
done
# A tier already running without the group keeps making files only root can read: it is
# restarted with --restart, and refused otherwise, rather than left so and called readable.
stale=()
for u in ebpfm.service ebpfm-node.service; do
  if _runs_without_group "$u"; then stale+=("$u"); fi
done
if [ ${#stale[@]} -gt 0 ]; then
  if [ "$restart" = 0 ]; then
    [ "$PLAN" = 1 ] && say "a run would stop here: ${stale[*]} started without EBPFM_GROUP=$GROUP (--restart)"
    [ "$PLAN" = 1 ] || die "${stale[*]} started without EBPFM_GROUP=$GROUP and read it only at start: every file
  they make from now on would be readable by root alone, the dashboard's included. Run this
  again with --restart to restart them (the eBPF tier then flushes every live process as
  \`truncated\`), or restart them yourself and run it again."
  else
    say "restarting ${stale[*]}, started without EBPFM_GROUP=$GROUP (the eBPF tier flushes its live processes as \`truncated\`)"
    run systemctl restart "${stale[@]}"
  fi
fi
run systemctl enable --now ebpfm.service ebpfm-node.service
install_env_save MADE_DEFAULTS MADE_GROUP RAN_BOOTSTRAP
if [ "$PLAN" = 1 ]; then
  say "the plan ends here: nothing was changed"
  exit 0
fi
say "the collector runs; its output is readable by group $GROUP"
bash "$EBPFM" status || true
