#!/bin/bash
# install-collector.sh [--no-bootstrap] [--no-check]: run the collector on this host under
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
# COLLECTOR_GROUP (ebpfm; a group /etc/default/ebpfm already names is kept). Run it again
# after a `git pull`: the units are written again, and a running collector keeps running --
# a restart flushes every live process as `truncated` -- so restart it when you choose:
#   systemctl restart ebpfm ebpfm-node
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
EBPFM=${EBPFM_SH:-$repo/collector/ebpfm.sh}
EBPF_DIR=${EBPF_DIR:-/var/log/ebpfm}
NODE_DIR=${NODE_DIR:-/var/log/ebpfm-login}
DEFAULTS=${EBPFM_DEFAULT_FILE:-/etc/default/ebpfm}
PACKAGED=${EBPFM_PACKAGED_UNIT:-/usr/lib/systemd/system/ebpfm.service}

say() { printf '==> %s\n' "$*"; }
die() { printf 'install-collector.sh: %s\n' "$*" >&2; exit 1; }

bootstrap=1 check=1
for a in "$@"; do
  case $a in
    --no-bootstrap) bootstrap=0 ;;
    --no-check) check=0 ;;
    *) echo "usage: install-collector.sh [--no-bootstrap] [--no-check]" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" = 0 ] || die "run as root: the eBPF tier loads programs into the kernel"
command -v systemctl >/dev/null 2>&1 || die "no systemctl: the collector runs as systemd units"
[ -f "$EBPFM" ] || die "no collector at $EBPFM"
for d in "$EBPF_DIR" "$NODE_DIR"; do
  case $d in /?*) ;; *) die "EBPF_DIR and NODE_DIR must be absolute paths (got '$d')" ;; esac
done
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
named=$( (sed -n 's/^EBPFM_GROUP=//p' "$DEFAULTS" || true) | tail -n 1)
GROUP=${COLLECTOR_GROUP:-${named:-ebpfm}}
if [ -n "$named" ] && [ "$named" != "$GROUP" ]; then
  die "$DEFAULTS gives the output to group $named, and COLLECTOR_GROUP says $GROUP; settle on one"
fi
getent group "$GROUP" >/dev/null || { say "group $GROUP"; groupadd --system "$GROUP"; }
if [ -z "$named" ]; then
  say "EBPFM_GROUP=$GROUP in $DEFAULTS"
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
systemctl enable --now ebpfm.service ebpfm-node.service
say "the collector runs; its output is readable by group $GROUP"
bash "$EBPFM" status || true
