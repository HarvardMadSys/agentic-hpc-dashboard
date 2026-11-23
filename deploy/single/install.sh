#!/bin/bash
# install.sh [--no-bootstrap] [--no-check] [--restart] [--no-build] [--plan] [--help]: one
# server runs the collector and the dashboard, the dashboard reading the collector's
# folders where it writes them. Run as root, from a checkout of this repository that any
# account can read (README.md).
#
#   1. deploy/common/install-collector.sh: both tiers as systemd units, their output
#      readable by one group (--no-bootstrap, --no-check, --restart).
#   2. deploy/common/install-dashboard.sh single: the dashboard as a systemd unit, as its
#      own account in that group, and its hourly check (--no-build).
#
# --plan prints what both would do and changes nothing. The settings either takes (EBPF_DIR,
# MEMORY_MAX, SLURM, SITE_NAME, ...) come from the environment, and are kept for the next
# run: `deploy/common/install-dashboard.sh --help` lists them. Then make the password:
# deploy/common/password.sh.
#
# Run it again after a `git pull`: the dashboard restarts on the new code; the collector
# keeps running until you restart it (systemctl restart ebpfm ebpfm-node).
set -euo pipefail
common=$(cd "$(dirname "$0")/../common" && pwd)
collector=() dashboard=()
for a in "$@"; do
  case $a in
    --no-bootstrap|--no-check|--restart) collector+=("$a") ;;
    --no-build) dashboard+=("$a") ;;
    --plan) collector+=("$a"); dashboard+=("$a") ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) echo "usage: install.sh [--no-bootstrap] [--no-check] [--restart] [--no-build] [--plan] [--help]" >&2; exit 2 ;;
  esac
done
# A yes/no setting the dashboard would refuse is refused before the collector is touched.
for v in SLURM ANONYMIZE; do
  case $(printf '%s' "${!v:-}" | tr '[:upper:]' '[:lower:]') in
    ''|1|0|true|false|yes|no|on|off) ;;
    *) echo "install.sh: $v='${!v}' is not one of 1/0, true/false, yes/no, on/off" >&2; exit 1 ;;
  esac
done
"$common/install-collector.sh" ${collector[@]+"${collector[@]}"}
"$common/install-dashboard.sh" single ${dashboard[@]+"${dashboard[@]}"}
