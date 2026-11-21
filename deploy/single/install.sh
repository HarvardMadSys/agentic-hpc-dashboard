#!/bin/bash
# install.sh [--no-bootstrap] [--no-check] [--restart] [--no-build]: one server runs the collector and
# the dashboard, the dashboard reading the collector's folders where it writes them. Run as
# root, from a checkout of this repository that any account can read (README.md).
#
#   1. deploy/common/install-collector.sh: both tiers as systemd units, their output
#      readable by one group (--no-bootstrap, --no-check, --restart).
#   2. deploy/common/install-dashboard.sh single: the dashboard as a systemd unit, as its
#      own account in that group (--no-build).
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
    *) echo "usage: install.sh [--no-bootstrap] [--no-check] [--restart] [--no-build]" >&2; exit 2 ;;
  esac
done
"$common/install-collector.sh" ${collector[@]+"${collector[@]}"}
"$common/install-dashboard.sh" single ${dashboard[@]+"${dashboard[@]}"}
