#!/bin/bash
# backup.sh [--to FILE] [--help]: what cannot be rebuilt, from the dashboard's host to
# FILE on this machine (default rc-dashboard-<host>-<date>.tar, mode 600). Run on this
# machine, with site.env filled in.
#
# On the dashboard's host, `rc-dashboard backup` writes, from the dashboard's own settings:
# the Trend tab's roll-up table (DASH_ROOT/cache/rollup/), the anonymization salt
# (DASH_ROOT/cache/anonymize.salt), the session key, the config and the password's hash.
# Everything else there is rebuilt from the copy of the nodes. `rc-dashboard restore
# --from FILE` puts it back (hpc/deploy/README.md, "Moving to another host").
set -euo pipefail
umask 077
here=$(cd "$(dirname "$0")" && pwd)
to=
while [ $# -gt 0 ]; do
  case $1 in
    --to) [ $# -ge 2 ] || { echo "usage: backup.sh [--to FILE]" >&2; exit 2; }; to=$2; shift 2 ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) echo "usage: backup.sh [--to FILE] [--help]" >&2; exit 2 ;;
  esac
done
# shellcheck source=/dev/null
. "${SITE_ENV:-$here/site.env}"
: "${DASH_SSH:?DASH_SSH is not set in site.env}" "${DASH_ROOT:?DASH_ROOT is not set in site.env}"
to=${to:-rc-dashboard-${DASH_NODE:-dash}-$(date +%Y%m%d-%H%M%S).tar}
R=$DASH_ROOT
ssh -i "${SSH_KEY:-$HOME/.ssh/id_ed25519}" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o ConnectTimeout=20 -o LogLevel=ERROR "$DASH_SSH" \
    "set -e; umask 077; f=\$(mktemp $R/.backup.XXXXXX); trap 'rm -f \$f' EXIT
     cd $R/current/dashboard && RC_DASH_CONFIG=$R/etc/dashboard.config.json \
       RC_DASH_PASSWORD_HASH_FILE=$R/etc/password.hash \
       $R/venv/bin/python -m rc_dashboard backup --to \$f >&2 && cat \$f" > "$to.part"
mv -f "$to.part" "$to"
echo "backup in $to"
