#!/bin/bash
# keep-running.sh stream|dashboard: one window of the rc-dashboard tmux session, which
# rc-tmux.service starts (tmux-session.sh). It runs one process and starts it again
# when it ends:
#
#   stream     fleet_stream.py receive, in rc-fleet-stream.scope, capped at 512M. Starts
#              again after 10 s, but not after exit 3 (its base folder is not what it
#              was), and stops after 5 starts in 10 min.
#   dashboard  rc_dashboard serve, in rc-dashboard.scope, capped at RC_DASH_MEMORY_MAX
#              (site.env's DASH_MEMORY_MAX; 7G when run by hand without it). Starts
#              again after 30 s, and stops after 4 starts in 6 h: each start refills
#              its history.
#
# The cap is MemoryMax: past it the kernel kills the process, and this loop starts it
# again. There is no MemoryHigh: with no swap, a soft cap stalls a process instead of
# killing it, and a stalled dashboard answers nothing for as long as it lasts.
#
# Each start first stops a scope left from before (its loop was killed, and it may hold
# the port), then runs the start checks (check-local.sh). A trip (11) or an unsafe
# folder (13) stops the loop; the hourly check starts any other stopped loop again.
#
# The process runs in a session of its own and writes its log; this window shows that
# log, so tmux never holds the process up. The process is stopped by stopping its
# scope: SIGTERM, then SIGKILL after 90 s. In the window:
#   Ctrl-C (or Ctrl-\) while it runs  stops it; it starts again after the pause
#   Ctrl-C during the pause           ends the loop; the window stays and shows why
# When tmux ends or rc-tmux.service stops, the loop stops the process and waits for it.
set -u
umask 077
ulimit -c 0
# The deployment's folder, which holds everything: rc-tmux.service sets it from
# site.env's DASH_ROOT, and there is no default. The tests point the run folder (data,
# cache, logs) and etc/ elsewhere; the session never sets those.
R=${RC_DASH_ROOT:-}
if [ -z "$R" ]; then
  echo "keep-running.sh: RC_DASH_ROOT is not set; rc-tmux.service sets it to the deployment's folder" >&2
  exit 2
fi
RUN=${RC_DASH_RUN:-$R}
ETC=${RC_FLEET_ETC:-$R/etc}
CHECK=${RC_KEEP_CHECK:-$(dirname "$0")/check-local.sh}
name=${1:-}
case $name in
  stream)
    scope=rc-fleet-stream; cap=512M; pause=10; burst=5; within=600
    checks=(--dirs-only --base-ok)
    dir=$HOME
    cmd=(/usr/bin/python3 "$R/current/hpc/fleet/fleet_stream.py" receive --config "$ETC/stream.json") ;;
  dashboard)
    scope=rc-dashboard; cap=${RC_DASH_MEMORY_MAX:-7G}; pause=30; burst=4; within=21600
    checks=(--dirs-only --base-ok --auth-set --port-free)
    dir=$R/current/dashboard
    cmd=("$R/venv/bin/python" -m rc_dashboard serve) ;;
  *) echo "usage: keep-running.sh stream|dashboard" >&2; exit 2 ;;
esac
pause=${RC_KEEP_PAUSE:-$pause}
[ -n "${RC_KEEP_CMD:-}" ] && cmd=(bash -c "$RC_KEEP_CMD")
log=$RUN/logs/$scope.log
MARKER=$RUN/TRIPPED

stamp() { date '+%F %T'; }
say() { printf '== %s %s: %s\n' "$(stamp)" "$name" "$*" >> "$log"; }
tripped() { [ -e "$MARKER" ] || [ -L "$MARKER" ]; }

# The folders first, since the log lives there; if they are not safe, say so here only.
if ! msg=$("$CHECK" --dirs-only 2>&1); then
  echo "== $(stamp) $name: $msg; stopped. See the runbook, 'When the stream trips'."
  exit 1
fi

tail -n 30 -F "$log" 2>/dev/null &
viewer=$!
child= sleeper= phase=idle stopping=0 pending=0 status=0
finish() {
  [ -n "$sleeper" ] && kill "$sleeper" 2>/dev/null
  sleep 0.3                     # time for the window to show the last lines
  kill "$viewer" 2>/dev/null
}
trap finish EXIT

# One loop per process: a second one (a window made twice) would fight over its scope.
# The lock is on the log, which the process keeps open, so no lock file is left behind.
exec 9>>"$log"
if ! flock -n 9; then
  say "another keep-running.sh $name already runs (it holds the lock on its log); this one stops"
  exit 1
fi

stop_child() {  # SIGTERM, then SIGKILL after 90 s: what stopping its scope does
  if systemctl --user is-active --quiet "$scope.scope"; then
    systemctl --user stop --no-block "$scope.scope"
  else
    kill -TERM "$child" 2>/dev/null       # not in its scope yet
  fi
}
on_int() {
  if [ "$phase" = running ]; then
    say "Ctrl-C: stopping it; it starts again in $pause s (Ctrl-C then keeps it stopped)"
    if [ -n "$child" ]; then stop_child; else pending=1; fi
  else
    say "Ctrl-C: it stays stopped. To start it: tmux -L rc-dashboard respawn-pane -t rc-dashboard:$name.0"
    stopping=1
    [ -n "$sleeper" ] && kill "$sleeper" 2>/dev/null
  fi
}
on_term() {
  say "the tmux session is ending: stopping it"
  stopping=1
  if [ "$phase" = running ]; then
    if [ -n "$child" ]; then stop_child; else pending=1; fi
  fi
  [ -n "$sleeper" ] && kill "$sleeper" 2>/dev/null
}
trap on_int INT QUIT
trap on_term TERM HUP

# etc/auth.env as systemd's EnvironmentFile= reads it: KEY=VALUE, the blanks around
# the value dropped, then one pair of matching quotes around it (deploy/README.md tells
# a hash to go in single quotes, and systemd takes them off). No shell expansion: a
# hash is full of `$` signs.
unquote() {
  local v=$1
  v=${v#"${v%%[![:space:]]*}"}
  v=${v%"${v##*[![:space:]]}"}
  if [ ${#v} -ge 2 ]; then
    case $v in
      \"*\"|\'*\') v=${v:1:${#v}-2} ;;
    esac
  fi
  printf '%s' "$v"
}
load_auth() {
  local line
  if [ -f "$ETC/auth.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case $line in RC_DASH_*=*) export "${line%%=*}=$(unquote "${line#*=}")" ;; esac
    done < "$ETC/auth.env"
  fi
  # The hash file that password.sh (rc-deploy password) writes: the dashboard reads it
  # again when it changes, so a new password needs no restart.
  if [ -z "${RC_DASH_PASSWORD_HASH_FILE:-}" ] && [ -s "$ETC/password.hash" ]; then
    export RC_DASH_PASSWORD_HASH_FILE="$ETC/password.hash"
    # The file is the password, as the start check reads it (check-local.sh): a hash
    # left in auth.env beside it (rollback.sh puts one back for a release from before
    # the hash file) is not handed on too, which the dashboard refuses.
    unset RC_DASH_PASSWORD_HASH
  fi
}

run_once() {    # start the process in its scope, and wait for it to end; sets status
  say "starting it (start ${#starts[@]} of at most $burst in $((within / 60)) min)"
  pending=0
  phase=running
  (
    exec 9>&-
    cd "$dir" || exit 1
    export PYTHONUNBUFFERED=1
    if [ "$name" = dashboard ]; then
      export RC_DASH_CONFIG="$ETC/dashboard.config.json" MALLOC_ARENA_MAX=2
      # The stream's status, so the Feeds tab and `rc-dashboard status` can tell a link
      # that is down from a collector that stopped.
      export RC_DASH_TRANSPORT_FILES="${RC_DASH_TRANSPORT_FILES:-$RUN/data/state/status.json}"
      [ -n "${RC_DASH_PATH:-}" ] && export PATH="$RC_DASH_PATH"
      load_auth
    fi
    exec setsid systemd-run --user --scope --quiet --collect --unit="$scope" \
      -p MemoryMax="$cap" -- "${cmd[@]}"
  ) >> "$log" 2>&1 < /dev/null &
  child=$!
  [ "$pending" = 1 ] && stop_child        # a signal came while it was being started
  while :; do
    wait "$child"
    status=$?
    kill -0 "$child" 2>/dev/null || break
  done
  # A signal can end the wait just as the process ends; then ask again for its status.
  wait "$child" 2>/dev/null
  s=$?
  [ "$s" != 127 ] && status=$s
  phase=idle
  child=
}

starts=()
while [ "$stopping" = 0 ]; do
  now=$(date +%s)
  recent=()
  for t in ${starts[@]+"${starts[@]}"}; do
    [ $((now - t)) -lt "$within" ] && recent+=("$t")
  done
  starts=(${recent[@]+"${recent[@]}"})
  if [ "${#starts[@]}" -ge "$burst" ]; then
    say "started $burst times in $((within / 60)) min; stopped. The hourly check starts it again, or: tmux -L rc-dashboard respawn-pane -t rc-dashboard:$name.0"
    exit 1
  fi
  starts+=("$now")
  # A process whose loop was killed runs on in its scope, and may hold the port.
  if systemctl --user is-active --quiet "$scope.scope"; then
    say "stopping $scope.scope, left from before"
    systemctl --user stop "$scope.scope"
  fi
  failed=0
  for c in "${checks[@]}"; do
    msg=$("$CHECK" "$c" 2>&1)
    rc=$?
    if [ "$rc" -ne 0 ]; then
      failed=$rc
      say "start check $c failed (exit $rc): $msg"
      break
    fi
  done
  if [ "$failed" = 11 ] || [ "$failed" = 13 ]; then
    say "stopped until the folder is fixed; see the runbook, 'When the stream trips'"
    exit 1
  fi
  [ "$stopping" = 1 ] && break
  if [ "$failed" = 0 ] && tripped; then
    say "the stream tripped (TRIPPED) after the start checks; not starting. See the runbook, 'When the stream trips'"
    exit 1
  fi
  if [ "$failed" = 0 ]; then
    run_once
    [ "$stopping" = 1 ] && break
    if [ "$name" = stream ] && [ "$status" = 3 ]; then
      say "it ended with exit 3: its base folder is not what it was; stopped. See the runbook, 'When the stream trips'"
      exit 1
    fi
    say "it ended (exit $status); it starts again in $pause s. Ctrl-C now keeps it stopped"
  else
    say "it tries again in $pause s. Ctrl-C now stops trying"
  fi
  phase=pausing
  end=$((SECONDS + pause))
  while [ "$stopping" = 0 ] && [ "$SECONDS" -lt "$end" ]; do
    sleep 1 &
    sleeper=$!
    wait "$sleeper"
    sleeper=
  done
  phase=idle
done
exit 0
