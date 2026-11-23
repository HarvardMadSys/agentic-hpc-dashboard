#!/bin/bash
# backup.sh [--to FILE] [--settings] [--help] | backup.sh --restore FILE: what cannot be
# rebuilt, kept in one file, and put back. Run as root on the dashboard's host.
#
#   --to FILE    `rc-dashboard backup`, run as the dashboard's account with its settings
#                (dashboard.env, paths.env): the Trend tab's roll-up table
#                (STATE/cache/rollup/), the anonymization salt (STATE/cache/anonymize.salt),
#                the session key, and the password's hash. FILE is mode 600, root's;
#                default rc-dashboard-<host>-<date>.tar here. Everything else the dashboard
#                keeps is rebuilt from the day-files.
#   --settings   also ETC as FILE.settings.tar: dashboard.env, fleet.env, install.env,
#                and in a fleet the pull key the nodes let in. Keep it as you keep keys.
#   --restore F  the dashboard stops, `rc-dashboard restore --from F --force` puts the file
#                back as its account, replacing what the host holds (an installed host already
#                has a session key and a roll-up table, which restore keeps without --force),
#                and the dashboard starts again (it refills its history). The password's
#                hash is the one file root puts in place, as password.sh does: ETC is
#                root's, and the account may only read it. A host that had no password
#                then reads the restored one: paths.env is written again to name it. A
#                restore that fails starts the dashboard again on what it had.
#
# In a fleet with FLEET_KEEP_DAYS=0, the copy (STATE/mirror) may hold days the nodes no
# longer have: keep it too when you move (deploy/README.md, "What to back up").
set -euo pipefail
umask 077
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)

say() { printf '==> %s\n' "$*"; }
die() { printf 'backup.sh: %s\n' "$*" >&2; exit 1; }
# shellcheck source=SCRIPTDIR/lib.sh
. "$here/lib.sh"

to= restore= settings=0
while [ $# -gt 0 ]; do
  case $1 in
    --to) [ $# -ge 2 ] || die "--to needs a file"; to=$2; shift 2 ;;
    --restore) [ $# -ge 2 ] || die "--restore needs a file"; restore=$2; shift 2 ;;
    --settings) settings=1; shift ;;
    -h|--help) show_help "$0"; exit 0 ;;
    *) echo "usage: backup.sh [--to FILE] [--settings] [--help] | backup.sh --restore FILE" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || die "run as root: the dashboard's state is its account's alone"
install_env_load SERVICE_USER ETC STATE OPT MODE
SERVICE_USER=${SERVICE_USER:-rc-dashboard} ETC=${ETC:-/etc/rc-dashboard}
STATE=${STATE:-/var/lib/rc-dashboard} OPT=${OPT:-/opt/rc-dashboard} MODE=${MODE:-}
VENV=$OPT/venv
[ -x "$VENV/bin/python" ] || die "no venv at $VENV: is the dashboard installed here?"

# The dashboard's settings, as systemd gives them to its unit.
envs=()
for f in "$ETC/dashboard.env" "$ETC/paths.env"; do
  [ -r "$f" ] || continue
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in RC_DASH_*=*) k=${line%%=*}; envs+=("$k=$(env_value "$f" "$k")") ;; esac
  done < "$f"
done
extra=()        # settings of this run alone, after (and over) the dashboard's own
dash() {        # ARGS: rc-dashboard, as the dashboard's account, with its settings
  runuser -u "$SERVICE_USER" -- env -C "$repo/dashboard" ${envs[@]+"${envs[@]}"} \
    ${extra[@]+"${extra[@]}"} "$VENV/bin/python" -m rc_dashboard "$@"
}

if [ -n "$restore" ]; then
  [ -r "$restore" ] || die "cannot read $restore"
  # A folder of the account's own, which the restore reads the backup from and puts the
  # password's hash in. Everything else goes where the dashboard keeps it, the account's,
  # as the dashboard would have written it.
  work=$(mktemp -d "$STATE/.restore.XXXXXX")
  trap 'rm -rf "$work"' EXIT
  cp "$restore" "$work/backup.tar"
  chown "$SERVICE_USER" "$work" "$work/backup.tar"
  # RC_DASH_PASSWORD_HASH emptied for the restore alone: beside the hash file it would
  # leave the restore no place for the file (and hash_file_name comments it out below).
  extra=("RC_DASH_PASSWORD_HASH_FILE=$work/password.hash" "RC_DASH_PASSWORD_HASH=")
  say "stopping the dashboard, putting $restore back"
  systemctl stop rc-dashboard.service || true
  if ! dash restore --from "$work/backup.tar" --force; then
    systemctl start rc-dashboard.service || true
    die "rc-dashboard restore failed (above): the dashboard starts again, on what it had and any file listed above as restored"
  fi
  started=0
  if [ -e "$work/password.hash" ]; then
    say "$ETC/password.hash: the restored password's hash (root's, read by $SERVICE_USER)"
    # Read as the account: root follows no link left in the account's own folder.
    new=$(mktemp "$ETC/.password.hash.XXXXXX")
    runuser -u "$SERVICE_USER" -- cat "$work/password.hash" > "$new"
    chown root:"$SERVICE_USER" "$new"
    chmod 640 "$new"
    mv -f "$new" "$ETC/password.hash"
    if hash_file_unread; then
      hash_file_name
      started=1
    fi
  fi
  [ "$started" = 1 ] || systemctl start rc-dashboard.service
  say "the dashboard runs again: it refills its history from the day-files"
  exit 0
fi

to=${to:-rc-dashboard-$(hostname -s)-$(date +%Y%m%d-%H%M%S).tar}
tmp=$(mktemp "$STATE/.backup.XXXXXX")
chown "$SERVICE_USER" "$tmp"
dash backup --to "$tmp" || { rm -f "$tmp"; die "rc-dashboard backup failed"; }
mv -f "$tmp" "$to"
# Moved out of the account's folder, it is a file of root's here, or refused: chown and
# chmod follow a link.
{ [ -f "$to" ] && [ ! -L "$to" ]; } || { rm -f "$to"; die "rc-dashboard backup left no file at $to"; }
chown root:root "$to"
chmod 600 "$to"
say "backup in $to"
if [ "$settings" = 1 ]; then
  tar -C "$(dirname "$ETC")" -cf "$to.settings.tar" "$(basename "$ETC")"
  chmod 600 "$to.settings.tar"
  say "settings in $to.settings.tar (it holds the pull key, in a fleet: keep it as you keep keys)"
fi
