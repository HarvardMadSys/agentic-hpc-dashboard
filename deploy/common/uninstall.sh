#!/bin/bash
# uninstall.sh [--purge] [--plan] [--help]: take off this host what deploy/'s installers
# put on it, the dashboard's host or a node alike. Run as root, from the checkout.
#
#   always    stops and removes the units (rc-dashboard, its memory drop-in,
#             rc-dashboard-check and its timer, rc-pull; ebpfm and ebpfm-node through
#             `ebpfm.sh uninstall-unit`), the venv and Python in OPT, and the sysctl
#             drop-in /etc/sysctl.d/99-ebpfm.conf when install-collector.sh's bootstrap
#             wrote it (the two kernel settings stay on until the next boot).
#   --purge   also the settings and the data: ETC (dashboard.env, paths.env, the
#             password's hash, fleet.env, pull_key, install.env), STATE (the history,
#             the Trend tab's roll-up table, the anonymization salt, a fleet's copy), the
#             accounts SERVICE_USER and PULL_USER, and the group and /etc/default/ebpfm
#             when install-collector.sh made them.
#
# Never removed: the collector's day-files (EBPF_DIR, NODE_DIR: research data, yours to
# remove), the packages bootstrap installed, the checkout, and your own
# ~/.config/rc-dashboard (the password). It says what stays, and how to remove it. Take a
# backup first: deploy/rc-deploy backup. --plan prints what it would do.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
DEFAULTS=${EBPFM_DEFAULT_FILE:-/etc/default/ebpfm}
SYSCTL_CONF=${EBPFM_SYSCTL_CONF:-/etc/sysctl.d/99-ebpfm.conf}

say() { printf '==> %s\n' "$*"; }
die() { printf 'uninstall.sh: %s\n' "$*" >&2; exit 1; }
# shellcheck source=SCRIPTDIR/lib.sh
. "$here/lib.sh"
# shellcheck source=SCRIPTDIR/collector-paths.sh
. "$here/collector-paths.sh"

purge=0
for a in "$@"; do
  case $a in
    --purge) purge=1 ;;
    --plan) PLAN=1 ;;
    -h|--help) show_help "$0"; exit 0 ;;
    *) echo "usage: uninstall.sh [--purge] [--plan] [--help]" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || [ "$PLAN" = 1 ] || die "run as root"
[ "$PLAN" = 1 ] && say "the plan: nothing below is done"
install_env_load SERVICE_USER PULL_USER ETC STATE OPT MADE_DEFAULTS MADE_GROUP RAN_BOOTSTRAP
SERVICE_USER=${SERVICE_USER:-rc-dashboard} PULL_USER=${PULL_USER:-rc-pull}
# What the install made is known only from install.env, which an install made
# before it existed does not have: unknown is not ours, so it stays, and is
# listed as staying (under set -u an unset one stopped this script half-way,
# after the units were already gone)
MADE_DEFAULTS=${MADE_DEFAULTS:-} MADE_GROUP=${MADE_GROUP:-} RAN_BOOTSTRAP=${RAN_BOOTSTRAP:-}
ETC=${ETC:-/etc/rc-dashboard} STATE=${STATE:-/var/lib/rc-dashboard} OPT=${OPT:-/opt/rc-dashboard}
# A folder this script removes comes from install.env or the environment, so a
# slip there (OPT=/opt, STATE=/var/lib) must not take a system folder with it:
# only a full path at least two folders deep, and never a system root, is removed.
deletable() {
  case $1 in /*/*) ;; *) return 1 ;; esac
  case $1 in *[[:space:]]*|*/..|*/../*) return 1 ;; esac
  case ${1%/} in
    /etc|/opt|/var|/var/lib|/var/log|/var/tmp|/usr|/usr/local|/home|/root|/srv|/tmp|/scratch|/boot|/bin|/sbin|/lib|/lib64|/run|/proc|/sys|/dev) return 1 ;;
  esac
}
for d in "$OPT" "$ETC" "$STATE"; do
  deletable "$d" || die "$d does not look like a folder of the dashboard's own (a full path, two folders deep, not a system folder): nothing removed"
done
FROM_EBPF_DIR=env FROM_NODE_DIR=env
collector_paths
group=$(_ebpfm_default EBPFM_GROUP)
kept=()

# ---- the dashboard's units
for u in rc-dashboard-check.timer rc-dashboard-check.service rc-pull.service rc-dashboard.service; do
  if [ -e "$UNIT_DIR/$u" ]; then
    say "$u"
    run systemctl disable --now "$u" || true
    run rm -f "$UNIT_DIR/$u"
  fi
done
if [ -d "$UNIT_DIR/rc-dashboard.service.d" ]; then
  run rm -rf "$UNIT_DIR/rc-dashboard.service.d"
fi
run systemctl daemon-reload

# ---- the collector's units, and its sysctls
if [ -e "$UNIT_DIR/ebpfm.service" ] || [ -e "$UNIT_DIR/ebpfm-node.service" ]; then
  say "the collector's units (ebpfm.sh uninstall-unit): live processes are written as \`truncated\`"
  run env EBPFM_UNIT_DIR="$UNIT_DIR" bash "$repo/collector/ebpfm.sh" uninstall-unit
fi
if [ "$RAN_BOOTSTRAP" = yes ] && [ -e "$SYSCTL_CONF" ]; then
  say "$SYSCTL_CONF (the two kernel settings stay on until the next boot)"
  run rm -f "$SYSCTL_CONF"
elif [ -e "$SYSCTL_CONF" ]; then
  kept+=("$SYSCTL_CONF (not known to be this install's: install.env does not say)")
fi

# ---- the code's runtime
if [ -d "$OPT" ]; then
  say "$OPT: the venv and its Python"
  run rm -rf "$OPT"
fi

# ---- settings, data, accounts
if [ "$purge" = 1 ]; then
  for d in "$ETC" "$STATE"; do
    if [ -e "$d" ]; then say "$d"; run rm -rf "$d"; fi
  done
  for u in "$SERVICE_USER" "$PULL_USER"; do
    if id "$u" >/dev/null 2>&1; then
      say "account $u"
      if [ "$u" = "$PULL_USER" ]; then run userdel -r "$u" || true; else run userdel "$u" || true; fi
    fi
  done
  if [ "$MADE_GROUP" = yes ] && [ -n "$group" ] && getent group "$group" >/dev/null; then
    say "group $group"
    run groupdel "$group" || true
  fi
  if [ "$MADE_DEFAULTS" = yes ] && [ -e "$DEFAULTS" ]; then
    say "$DEFAULTS"
    run rm -f "$DEFAULTS"
  fi
  [ "$INSTALL_ENV" != "$ETC/install.env" ] && [ -e "$INSTALL_ENV" ] && run rm -f "$INSTALL_ENV"
else
  for d in "$ETC" "$STATE"; do [ -e "$d" ] && kept+=("$d"); done
  for u in "$SERVICE_USER" "$PULL_USER"; do id "$u" >/dev/null 2>&1 && kept+=("account $u"); done
  [ -e "$DEFAULTS" ] && kept+=("$DEFAULTS")
  [ "$MADE_GROUP" = yes ] && [ -n "$group" ] && kept+=("group $group")
fi
for d in "$EBPF_DIR" "$NODE_DIR"; do [ -e "$d" ] && kept+=("$d (the collector's day-files)"); done

if [ ${#kept[@]} -gt 0 ]; then
  say "these stay:"
  for k in "${kept[@]}"; do echo "      $k"; done
  [ "$purge" = 1 ] || echo "    --purge takes the settings, the data, the accounts and the group too."
  echo "    Day-files are research data, never removed here: rm -rf them yourself when done."
fi
[ "$PLAN" = 1 ] && say "the plan ends here: nothing was changed"
exit 0
