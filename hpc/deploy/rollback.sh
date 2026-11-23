#!/bin/bash
# rollback.sh [SHA] [--plan] [--help]: the dashboard's host back to the release it ran
# before (or to SHA, one of DASH_ROOT/releases), from this machine. Run on this machine,
# with site.env filled in.
#
# On the dashboard's host, `current` moves to that release, its pinned requirements go
# into the venv, and the session restarts (the dashboard refills its history). Each
# install-remote.sh notes its release in DASH_ROOT/releases/.history, which says which one
# came before. The settings stay as the last deploy rendered them, and the nodes keep
# their sender: a sender newer than the receiver is the order deploy.sh keeps anyway. To
# go back for good, check out the older commit and run deploy.sh --restart.
# --plan prints the release it would go back to, and changes nothing.
set -euo pipefail
umask 077
here=$(cd "$(dirname "$0")" && pwd)
want= plan=0
for a in "$@"; do
  case $a in
    --plan) plan=1 ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    -*) echo "usage: rollback.sh [SHA] [--plan] [--help]" >&2; exit 2 ;;
    *) [[ $a =~ ^[0-9a-f]{12}$ ]] || { echo "rollback.sh: $a is not a release (12 hex digits)" >&2; exit 2; }
       want=$a ;;
  esac
done
# shellcheck source=/dev/null
. "${SITE_ENV:-$here/site.env}"
: "${DASH_SSH:?DASH_SSH is not set in site.env}" "${DASH_ROOT:?DASH_ROOT is not set in site.env}"
ssh -i "${SSH_KEY:-$HOME/.ssh/id_ed25519}" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o ConnectTimeout=20 -o LogLevel=ERROR "$DASH_SSH" "bash -s -- $DASH_ROOT '$want' $plan" <<'REMOTE'
set -euo pipefail
R=$1 want=$2 plan=$3
cur=$(readlink "$R/current" 2>/dev/null | sed 's#^releases/##')
if [ -z "$want" ]; then
  # the release installed before the current one, newest first
  want=$( (tac "$R/releases/.history" 2>/dev/null || true) | awk '{print $1}' \
          | grep -vx "$cur" | head -n 1 || true)
fi
if [ -z "$want" ] || [ ! -e "$R/releases/$want/.installed" ]; then
  echo "rollback.sh: no release to go back to (current: ${cur:-none}); DASH_ROOT/releases holds:" >&2
  ls -1 "$R/releases" >&2 || true
  exit 1
fi
if [ "$want" = "$cur" ]; then
  echo "release $want is current already"
  exit 0
fi
if [ "$plan" = 1 ]; then
  echo "would go back from ${cur:-none} to $want, and restart the session"
  exit 0
fi
"$R/venv/bin/python" -m pip install --quiet --require-hashes -r "$R/releases/$want/requirements.txt"
ln -sfn "releases/$want" "$R/current.new"
mv -T "$R/current.new" "$R/current"
printf '%s %s rollback\n' "$want" "$(date '+%F %T')" >> "$R/releases/.history"
systemctl --user restart rc-tmux.service
echo "current: $want (was ${cur:-none}); the session restarted"
REMOTE
