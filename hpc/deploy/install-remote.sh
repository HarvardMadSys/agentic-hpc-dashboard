#!/bin/bash
# install-remote.sh SHA FIRST RESTART ROOT [PYTHON]: run on the dashboard's host by
# deploy.sh (as `bash -s` over ssh), on the payload it unpacked into ROOT/incoming/SHA.
# ROOT is the deployment's folder (site.env's DASH_ROOT), which holds everything, and
# PYTHON (site.env's DASH_PYTHON, python3 if not given) makes the venv. Installs the
# release, moves `current` to it, installs the settings deploy.sh rendered and the tmux
# unit, keeps etc/auth.env and etc/password.hash, notes the release in
# releases/.history (rollback.sh), and starts the session (or restarts it with RESTART=1).
set -euo pipefail
umask 077
if [ $# -lt 4 ] || [ $# -gt 5 ] || [ -z "$4" ]; then
  echo "usage: install-remote.sh SHA FIRST RESTART ROOT [PYTHON]" >&2
  exit 2
fi
sha=$1 first=$2 restart=$3 R=$4 py=${5:-python3}
inc=$R/incoming/$sha
rel=$R/releases/$sha
# site.env's PRIVATE_TREE, in the kit.env this release ships. "no" (a host that only the
# team can log in to): the modes and ACLs the user set stay as they are; only etc/,
# which holds the secrets, is made 700.
private=$(sed -n 's/^PRIVATE_TREE=//p' "$inc/kit.env" 2>/dev/null | tail -n 1)
[ "$private" = no ] || private=yes
mkdir -p "$R/releases" "$R/etc" "$HOME/.config/systemd/user"
if [ "$private" = yes ]; then
  chmod 700 "$R" "$R/releases" "$R/etc"
else
  chmod 700 "$R/etc"
fi

if [ -e "$rel/.installed" ]; then
  # The same sha again, as a settings change ships it: the folder already holds this
  # release, and a running process may use it, so it stays as it is.
  echo "release $sha already installed; kept as it is"
else
  # Unpacked beside it and renamed into place, so releases/SHA is never half-written.
  rm -rf "$rel" "$rel.tmp"
  mkdir -p "$rel.tmp"
  tar -C "$rel.tmp" --strip-components=1 --no-same-permissions -xf "$inc/release.tar"
  cp -r "$inc/dist" "$rel.tmp/dist"
  install -m 600 "$inc/requirements.txt" "$rel.tmp/requirements.txt"
  touch "$rel.tmp/.installed"
  mv -T "$rel.tmp" "$rel"
fi

# A venv made by another Python than DASH_PYTHON -- site.env moved to a newer one -- is
# made again: its packages are built for its own version. The old one is kept beside it
# (venv.old) until the next such move, and the session restarts: the running dashboard's
# packages are no longer where it found them, and a worker process it starts
# (ingest.workers) would start from the new venv, not its own.
want=$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
have=$("$R/venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
# Made again only when both versions are known and differ: a venv whose Python does not
# answer is left as it was before this check existed.
other=0
if [ -n "$want" ] && [ -n "$have" ] && [ "$want" != "$have" ]; then other=1; fi
if [ "$first" = 1 ] || [ ! -x "$R/venv/bin/python" ] || [ "$other" = 1 ]; then
  if ! "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    echo "install-remote.sh: $py is not Python 3.11 or later; set DASH_PYTHON in site.env" >&2
    exit 1
  fi
  if [ -x "$R/venv/bin/python" ] && [ "$other" = 1 ]; then
    echo "the venv is Python $have, DASH_PYTHON is $want: making it again, and restarting"
    rm -rf "$R/venv.old"
    mv -T "$R/venv" "$R/venv.old"
    restart=1
  fi
  "$py" -m venv "$R/venv"
  "$R/venv/bin/python" -m pip install --quiet --upgrade pip
fi
"$R/venv/bin/python" -m pip install --quiet --require-hashes -r "$rel/requirements.txt"

ln -sfn "releases/$sha" "$R/current.new"
mv -T "$R/current.new" "$R/current"
# Which release came before which, for rollback.sh.
printf '%s %s\n' "$sha" "$(date '+%F %T')" >> "$R/releases/.history"

# The copy's settings (pull.py), and what the kit's scripts read. An etc/stream.json from
# the stream this replaced stays: a rollback to a release before it reads it again.
install -m 600 "$inc/fleet.env" "$R/etc/fleet.env"
install -m 600 "$inc/kit.env" "$R/etc/kit.env"
if [ -e "$R/TRIPPED" ]; then
  echo "note: $R/TRIPPED, where the stream before the copy stopped both processes, is no longer read;" \
       "its reason is in $R/logs/rc-fleet-stream.log. Remove it once read (a rollback to a release" \
       "before the copy would stop at it again)."
fi
install -m 600 "$inc/dashboard.config.json" "$R/etc/dashboard.config.json"
[ -e "$R/etc/auth.env" ] || install -m 600 /dev/null "$R/etc/auth.env"
# The session's windows run the kit's scripts by the path the unit named, through
# `current`. A release whose kit is in another folder (deploy/fleet became hpc/deploy)
# has no such path, so a window whose process ended could not start it again, and the
# hourly check's respawn would fail the same way: a unit whose ExecStart changed
# restarts the session.
unit=$HOME/.config/systemd/user/rc-tmux.service
if [ -e "$unit" ] && [ "$(grep '^ExecStart=' "$unit" || true)" != \
     "$(grep '^ExecStart=' "$inc/rc-tmux.service" || true)" ]; then
  echo "the unit's ExecStart changed (the kit moved): restarting the session"
  restart=1
fi
install -m 600 "$inc/rc-tmux.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable rc-tmux.service >/dev/null 2>&1 || true
# A session without its two windows (a deploy that stopped half way, one someone closed,
# or one from before the copy replaced the stream, whose window was `stream`) must
# restart: a start would keep it as it is.
again=0
for w in copy dashboard; do
  tmux -L rc-dashboard list-panes -t "rc-dashboard:$w" >/dev/null 2>&1 || again=1
done
# pip writes executables and libraries with fixed modes (755, 711) whatever the umask;
# owner-only everywhere keeps the hourly audit quiet and the tree private. Done before the
# session starts: each window's start checks judge the folders (check-local.sh), and one
# that an earlier release left open would stop both until the next hourly check.
if [ "$private" = yes ]; then
  chmod -R go-rwx "$R"
fi
# Start the session (a no-op when it runs), then turn linger on, so it outlives this
# login.
if [ "$restart" = 1 ] || [ "$again" = 1 ]; then
  systemctl --user restart rc-tmux.service || echo "rc-tmux did not start; see check-local.sh"
else
  systemctl --user start rc-tmux.service || echo "rc-tmux did not start; see check-local.sh"
  # The running copy reads etc/fleet.env again on SIGHUP (pull.py's reload): a node added
  # starts, one taken out stops, and nothing restarts. Its lock file holds its pid, which
  # is signalled while it is still a pull.py loop: asked without taking the lock, which a
  # copy starting this moment would find held and stop at.
  pid=$(head -n 1 "$R/data/mirror/.state/pull.lock" 2>/dev/null || true)   # none yet: no copy has run
  case $pid in
    ''|*[!0-9]*) ;;
    *)
      if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q 'pull\.py .*loop'; then
        kill -HUP "$pid" 2>/dev/null && echo "the copy reads its new settings (pid $pid)"
      fi ;;
  esac
fi
loginctl enable-linger 2>/dev/null || true
rm -rf "$inc" 2>/dev/null || echo "note: could not remove $inc yet (NFS); harmless"
echo "installed $sha"
