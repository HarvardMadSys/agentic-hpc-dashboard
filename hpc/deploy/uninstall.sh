#!/bin/bash
# uninstall.sh [--purge] [--plan] [--help]: take the deployment off every machine it is
# on, from this machine, with site.env filled in. What hpc/deploy/README.md, "Removing
# everything", lists, done for you:
#
#   the dashboard's host  the session stops and its unit goes (rc-tmux.service); the
#                         releases, the venv, and `current` go. --purge: all of DASH_ROOT
#                         (the copy, the history, the Trend tab's roll-up table, the
#                         settings, the password's hash, the stream key).
#   the nodes             the sender goes (SENDER_DIR), and the stream key's line in
#                         SOURCE_USER's ~/.ssh/authorized_keys (the one that runs
#                         fleet_stream.py send); every other line stays.
#   this machine          the hourly check's crontab line. --purge: WORK_DIR too, and
#                         ~/.config/rc-dashboard (the password and its netrc).
#
# Linger stays on (loginctl disable-linger, unless something else of yours needs it), and
# the collector on the nodes is the site's. Take a backup first: backup.sh. --plan prints
# what it would do.
set -uo pipefail
umask 077
here=$(cd "$(dirname "$0")" && pwd)
purge=0 plan=0
for a in "$@"; do
  case $a in
    --purge) purge=1 ;;
    --plan) plan=1 ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) echo "usage: uninstall.sh [--purge] [--plan] [--help]" >&2; exit 2 ;;
  esac
done
# shellcheck source=/dev/null
. "${SITE_ENV:-$here/site.env}"
: "${DASH_SSH:?DASH_SSH is not set in site.env}" "${DASH_ROOT:?DASH_ROOT is not set in site.env}"
ssh_=(ssh -i "${SSH_KEY:-$HOME/.ssh/id_ed25519}" -o IdentitiesOnly=yes -o BatchMode=yes
      -o ConnectTimeout=20 -o LogLevel=ERROR)
say() { printf '==> %s\n' "$*"; }
remote() {      # TARGET COMMAND
  if [ "$plan" = 1 ]; then printf '    would run on %s: %s\n' "$1" "$2"; return 0; fi
  "${ssh_[@]}" "$1" "$2"
}
failed=0
R=${DASH_ROOT%/}
# site.env is read here as bash reads it, not through render.py's checks, and
# what it names is removed with rm -rf on other hosts: refuse anything but a
# plain full path at least two folders deep, as render.py requires of a
# deployment, before a slip (DASH_ROOT=/home, SENDER_DIR=~) can remove a home.
plain_deep() {
  case $1 in /*/*) ;; *) return 1 ;; esac
  case $1 in *[[:space:]]*|*\'*|*\"*|*\\*|*'$'*|*'`'*|*';'*|*'&'*|*'|'*|*/..|*/../*) return 1 ;; esac
}
plain_deep "$R" || { echo "DASH_ROOT=$DASH_ROOT is not a plain full path two folders deep: nothing removed" >&2; exit 2; }
if [ -n "${SENDER_DIR:-}" ]; then
  plain_deep "${SENDER_DIR%/}" || { echo "SENDER_DIR=$SENDER_DIR is not a plain full path two folders deep: nothing removed" >&2; exit 2; }
fi
say "the dashboard's host, ${DASH_NODE:-$DASH_SSH}"
# --purge takes the whole DASH_ROOT, and only one that looks like a deployment --
# the releases folder and the `current` link a deploy makes together -- and
# never the account's home itself
if [ "$purge" = 1 ]; then
  gone="$R"
  guard="{ [ -d $R/releases ] && [ -e $R/current ] && [ \"$R\" != \"\$HOME\" ]; } || { echo '$R is not a deployment (no releases and current), or is the home: not removed' >&2; exit 3; }; "
else
  gone="$R/releases $R/current $R/venv $R/venv.old $R/incoming"
  guard=""
fi
remote "$DASH_SSH" "systemctl --user disable --now rc-tmux.service 2>/dev/null; rm -f \$HOME/.config/systemd/user/rc-tmux.service; systemctl --user daemon-reload; ${guard}rm -rf $gone" \
  || { echo "  the dashboard's host: FAILED" >&2; failed=1; }
if [ -n "${SENDER_DIR:-}" ]; then
  for node in $SOURCE_NODES; do
    dest="$SOURCE_USER@$node${SOURCE_DOMAIN:+.$SOURCE_DOMAIN}"
    say "$node: the sender, and the stream key's line"
    if remote "$dest" "rm -rf $SENDER_DIR; ak=\$HOME/.ssh/authorized_keys; if [ -f \$ak ]; then grep -v 'fleet_stream.py send' \$ak > \$ak.new; chmod 600 \$ak.new; mv -f \$ak.new \$ak; fi"; then
      [ "${SHARED_HOME:-yes}" = yes ] && break
    else
      echo "  $node: FAILED" >&2
      failed=1
    fi
  done
fi
say "this machine"
if [ "$plan" = 1 ]; then
  echo "    would take the line naming ${WORK_DIR:-WORK_DIR}/bin/check.sh out of the crontab"
else
  if current=$(crontab -l 2>/dev/null); then
    # read first, then written: `crontab -` must not truncate what `crontab -l` reads
    { printf '%s\n' "$current" | grep -vF "${WORK_DIR:-/nonexistent}/bin/check.sh" || true; } \
      | crontab - || failed=1
  fi
fi
if [ "$purge" = 1 ]; then
  for d in "${WORK_DIR:-}" "$HOME/.config/rc-dashboard"; do
    [ -n "$d" ] || continue
    if [ "$plan" = 1 ]; then echo "    would run: rm -rf $d"; else rm -rf "$d"; fi
  done
else
  echo "    kept: $R/{etc,cache,data,logs} on the dashboard's host (--purge takes them), WORK_DIR, ~/.config/rc-dashboard"
fi
echo "    linger stays on: loginctl disable-linger on the dashboard's host, unless something else of yours needs it"
exit "$failed"
