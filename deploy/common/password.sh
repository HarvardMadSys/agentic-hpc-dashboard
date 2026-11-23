#!/bin/bash
# password.sh [--rotate] [--plan] [--help]: the dashboard's password, made and put in place
# in one step. Run as root (sudo) on the dashboard's host, from the checkout, after
# install-dashboard.sh (deploy/single/install.sh, deploy/fleet/install-dashboard.sh).
#
#   1. The password is yours: it is kept in your ~/.config/rc-dashboard/password (the
#      account sudo was run from), with a netrc for curl beside it, both mode 600, by
#      `rc-dashboard new-password`. One already there is kept, unless --rotate makes a
#      new one, which signs everyone out.
#   2. Its hash goes in ETC/password.hash (0640, root:rc-dashboard), which the dashboard
#      reads again whenever it changes: a new password needs no restart.
#   3. The first time, paths.env is written again to name the hash file, and the dashboard
#      starts (or restarts): install-dashboard.sh --no-build, in the mode install.env
#      names. A RC_DASH_PASSWORD_HASH left in dashboard.env is commented out, since the
#      hash file is the one to keep.
#
# To share the page, give colleagues the password from that file, over a channel you
# trust; to take it back from everyone, --rotate. --plan prints what it would do.
# Reads ETC, OPT, SERVICE_USER and MODE from /etc/rc-dashboard/install.env.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)

say() { printf '==> %s\n' "$*"; }
die() { printf 'password.sh: %s\n' "$*" >&2; exit 1; }
# shellcheck source=SCRIPTDIR/lib.sh
. "$here/lib.sh"

rotate=0
for a in "$@"; do
  case $a in
    --rotate) rotate=1 ;;
    --plan) PLAN=1 ;;
    -h|--help) show_help "$0"; exit 0 ;;
    *) echo "usage: password.sh [--rotate] [--plan] [--help]" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || [ "$PLAN" = 1 ] || die "run as root (sudo): the hash goes in a folder only root writes"
install_env_load ETC OPT SERVICE_USER MODE
ETC=${ETC:-/etc/rc-dashboard} OPT=${OPT:-/opt/rc-dashboard}
SERVICE_USER=${SERVICE_USER:-rc-dashboard} MODE=${MODE:-}
VENV=$OPT/venv
[ -x "$VENV/bin/python" ] || [ "$PLAN" = 1 ] || die "no venv at $VENV: run the installer first"
[ -d "$ETC" ] || [ "$PLAN" = 1 ] || die "no $ETC: run the installer first"

# Whose password: the account sudo was run from, else root.
who=${SUDO_USER:-root}
[ "$who" = root ] || id "$who" >/dev/null 2>&1 || who=root
home=$( (getent passwd "$who" || true) | cut -d: -f6)
home=${home:-/root}
dir=$home/.config/rc-dashboard
mine=$dir/password.hash
as_user() { if [ "$who" = root ]; then "$@"; else runuser -u "$who" -- "$@"; fi; }

if [ "$rotate" = 1 ] || [ ! -e "$dir/password" ]; then
  say "a new password for $who, in $dir/password"
  force=()
  [ "$rotate" = 1 ] && force=(--force)
  run as_user env -C "$repo/dashboard" "$VENV/bin/python" -m rc_dashboard new-password \
    ${force[@]+"${force[@]}"} --dir "$dir" --install "$mine"
elif [ ! -s "$mine" ]; then
  # A password made before hash files: its hash, from the password itself.
  say "the hash of $who's password in $dir/password"
  run as_user env -C "$repo/dashboard" "$VENV/bin/python" -c '
import os, sys
from rc_dashboard import auth
pw = open(sys.argv[1]).read().strip()
old = os.umask(0o077)
with open(sys.argv[2], "w") as fh:
    fh.write(auth.hash_password(pw) + "\n")
os.umask(old)' "$dir/password" "$mine"
fi
say "$ETC/password.hash: the dashboard reads it again when it changes"
run install -m 640 -o root -g "$SERVICE_USER" "$mine" "$ETC/password.hash"

if hash_file_unread; then
  hash_file_name
else
  say "the dashboard reads the new hash within a few seconds; everyone signs in again"
fi
say "the password is in $dir/password (for curl: --netrc-file $dir/netrc)"
