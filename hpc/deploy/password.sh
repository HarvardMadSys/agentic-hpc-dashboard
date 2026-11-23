#!/bin/bash
# password.sh [--rotate] [--plan] [--help]: the dashboard's password, made on this machine
# and its hash put on the dashboard's host, in one step. Run on this machine, from the
# checkout, with site.env filled in.
#
#   1. The password is yours: ~/.config/rc-dashboard/password, with a netrc for curl
#      beside it, both mode 600 (`rc-dashboard new-password`, run from this checkout with
#      uv). One already there is kept, unless --rotate makes a new one: everyone signs in
#      again.
#   2. Its hash goes to DASH_ROOT/etc/password.hash on the dashboard's host (mode 600),
#      which the dashboard reads again whenever it changes: no restart, and no hash to
#      paste into a file by hand. deploy.sh runs this when the host has no hash yet. A
#      hash left in etc/auth.env is commented out, and the dashboard restarts once (its
#      window starts it again) to read the file alone.
#
# To share the page, give colleagues the password from that file, over a channel you
# trust; to take it back from everyone, --rotate. --plan prints what it would do.
set -euo pipefail
umask 077
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
PLAN=0 rotate=0
for a in "$@"; do
  case $a in
    --rotate) rotate=1 ;;
    --plan) PLAN=1 ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) echo "usage: password.sh [--rotate] [--plan] [--help]" >&2; exit 2 ;;
  esac
done
# shellcheck source=/dev/null
. "${SITE_ENV:-$here/site.env}"
: "${DASH_SSH:?DASH_SSH is not set in site.env}" "${DASH_ROOT:?DASH_ROOT is not set in site.env}"
SSH_KEY=${SSH_KEY:-$HOME/.ssh/id_ed25519}
dir=$HOME/.config/rc-dashboard
mine=$dir/password.hash

say() { printf '==> %s\n' "$*"; }
run() {
  if [ "$PLAN" = 1 ]; then printf '    would run: %s\n' "$*"; return 0; fi
  "$@"
}
dash() { (cd "$repo/dashboard" && uv run --frozen python "$@"); }

if [ "$rotate" = 1 ] || [ ! -e "$dir/password" ]; then
  say "a new password, in $dir/password"
  force=()
  [ "$rotate" = 1 ] && force=(--force)
  run dash -m rc_dashboard new-password ${force[@]+"${force[@]}"} --dir "$dir" --install "$mine"
elif [ ! -s "$mine" ]; then
  # A password made before hash files: its hash, from the password itself.
  say "the hash of the password in $dir/password"
  run dash -c '
import os, sys
from rc_dashboard import auth
pw = open(sys.argv[1]).read().strip()
os.umask(0o077)
with open(sys.argv[2], "w") as fh:
    fh.write(auth.hash_password(pw) + "\n")' "$dir/password" "$mine"
fi
say "its hash to $DASH_ROOT/etc/password.hash on ${DASH_NODE:-$DASH_SSH}: the dashboard reads it again when it changes"
ssh_=(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20 -o LogLevel=ERROR)
E=$DASH_ROOT/etc
if [ "$PLAN" = 1 ]; then
  run ssh "$DASH_SSH" "cat > $E/password.hash"
else
  # The hash file replaces a hash in auth.env (as earlier versions had it): that line is
  # commented out, and the dashboard started from it restarts once, to read the file.
  moved=$("${ssh_[@]}" "$DASH_SSH" "umask 077; mkdir -p $E && cat > $E/.password.hash.new && mv -f $E/.password.hash.new $E/password.hash
    if grep -q '^RC_DASH_PASSWORD_HASH=' $E/auth.env 2>/dev/null; then
      sed 's/^RC_DASH_PASSWORD_HASH=/# replaced by password.hash: RC_DASH_PASSWORD_HASH=/' $E/auth.env > $E/.auth.env.new
      chmod 600 $E/.auth.env.new && mv -f $E/.auth.env.new $E/auth.env && echo moved
    fi" < "$mine")
  if [ "$moved" = moved ]; then
    say "the hash in $E/auth.env is commented out; the dashboard restarts once to read the file"
    "${ssh_[@]}" "$DASH_SSH" "systemctl --user stop rc-dashboard.scope" || true
  fi
fi
say "the password is in $dir/password (for curl through a tunnel: --netrc-file $dir/netrc)"
