#!/bin/bash
# check-local.sh: run on the dashboard's host, as the account that owns the deployment,
# with RC_DASH_ROOT set to the deployment's folder (rc-tmux.service and check.sh set it).
#
#   check-local.sh --dirs-only   the folder and its cache/, logs/, and data/ are real 700
#                                folders owned by this account (created if missing);
#                                exit 0 or 13
#   check-local.sh --base-ok     the folder and its data/ (the stream's base) are real 700
#                                folders owned by this account, and the stream has not
#                                tripped (no TRIPPED in the folder); exit 0, 11, or 13
#   check-local.sh --auth-set    etc/auth.env holds a password hash; exit 0 or 15
#   check-local.sh --port-free   nothing listens on the dashboard's port yet; exit 0 or 14
#   check-local.sh               the hourly check (check.sh runs it over ssh): all of the
#                                above, linger, the tmux session and its two windows (a
#                                window whose loop ended is started again), a dashboard
#                                that stopped answering, every stream's health, log
#                                rotation, and the privacy audit. One line out.
#
# keep-running.sh also runs the start checks before each start of its process.
#
# With site.env's PRIVATE_TREE=false (etc/stream.json's "private"), "a 700 folder owned by
# this account" below reads "a real folder", and there is no privacy audit.
#
# Exit codes, most severe first when several apply: 11 TRIPPED (the stream stopped on a
# swapped base folder; nothing is restarted), 13 UNSAFE (a folder is not this account's
# 700 folder), 14 PORT (someone else listens on the port), 15 AUTH (no password
# hash, or a start failed), 16 STREAM (a source node down over an hour, or a stale
# status), 12 AUDIT (files readable by others, or extended ACLs), 10 RESTARTED, FROZEN,
# or LINGER (something was started or restarted, or linger was turned back on), 0 OK;
# 2 for a usage error, or no RC_DASH_ROOT.
set -u
umask 077
me=$(id -un)
uid=$(id -u)
# Everything lives in the deployment's folder; there is no default. The tests point the
# run folder (data, cache, logs), the stream's base, and etc/ elsewhere; the session
# never sets those.
R=${RC_DASH_ROOT:-}
if [ -z "$R" ]; then
  echo "check-local.sh: RC_DASH_ROOT is not set; it names the deployment's folder" >&2
  exit 2
fi
RUN=${RC_DASH_RUN:-$R}
BASE=${RC_FLEET_BASE:-$RUN/data}
ETC=${RC_FLEET_ETC:-$R/etc}
MARKER=$RUN/TRIPPED
# The tmux session that runs both processes, its windows (tmux-session.sh), and the
# loop each window runs.
UNIT=rc-tmux.service
WINDOWS="stream dashboard"
KEEP=$(dirname "$0")/keep-running.sh
CGROUP_ROOT=${RC_CGROUP_ROOT:-/sys/fs/cgroup}
DOWN_ALERT_S=3600          # a source node down longer than this is reported
STALE_ALERT_S=600          # a status file older than this, with the stream running, too
FROZEN_AFTER_S=1200        # a listening dashboard this old must answer
NEVER_LISTENED_S=7200      # one not listening yet gets this long: a start on a busy core is slow
RETRY_S=${RC_CHECK_RETRY_S:-30}
# The dashboard's port, from its settings; the dashboard's own default without them.
PORT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["server"]["port"])' \
       "$ETC/dashboard.config.json" 2>/dev/null || echo 8080)
# site.env's PRIVATE_TREE, as deploy.sh renders it into etc/stream.json. "no" (a host that
# only the team can log in to): modes, owners, and ACLs are the user's business, so the
# folders must only be real folders, and the privacy audit is off. Anything unreadable
# counts as "yes".
PRIVATE=$(python3 -c 'import json,sys; print("no" if json.load(open(sys.argv[1])).get("private") is False else "yes")' \
          "$ETC/stream.json" 2>/dev/null || echo yes)
if [ "$PRIVATE" = no ]; then
  WANT="a real folder"
else
  WANT="a 700 folder owned by $me"
fi

private_dir() {  # a real folder (not a symlink); unless PRIVATE=no, this account's, mode 700
  [ -d "$1" ] && [ ! -L "$1" ] || return 1
  [ "$PRIVATE" = no ] && return 0
  [ "$(stat -c '%u %a' "$1")" = "$uid 700" ]
}
own_dir() {      # the same, made if missing
  # Both windows' loops check at the same moment when the session starts, so on a new
  # folder one mkdir can fail because the other check made the folder first. What is
  # there is then judged as it is: this account's 700 folder, or unsafe.
  if [ ! -e "$1" ] && [ ! -L "$1" ]; then
    mkdir -m 700 "$1" 2>/dev/null
  fi
  private_dir "$1"
}
dirs_ok() { own_dir "$RUN" && own_dir "$RUN/cache" && own_dir "$RUN/logs" && own_dir "$BASE"; }
base_ok() { private_dir "$RUN" && private_dir "$BASE"; }
tripped() { [ -e "$MARKER" ] || [ -L "$MARKER" ]; }
auth_set() { grep -q '^RC_DASH_PASSWORD_HASH=..' "$ETC/auth.env" 2>/dev/null; }
listener() { ss -ltnpH "sport = :$PORT" 2>/dev/null; }
tmx() { tmux -L rc-dashboard "$@"; }
scope_pids() {   # the processes of a running scope, one per line
  local cg
  systemctl --user is-active --quiet "$1" || return 0
  cg=$(systemctl --user show -p ControlGroup --value "$1" 2>/dev/null)
  [ -n "$cg" ] && cat "$CGROUP_ROOT$cg/cgroup.procs" 2>/dev/null
  return 0
}
answers() {      # the dashboard answers at all; a 401, without the password, counts
  local c
  c=$(curl -s --noproxy '*' -o /dev/null -m 60 -w '%{http_code}' "http://127.0.0.1:$PORT/api/health" 2>/dev/null)
  [ -n "$c" ] && [ "$c" != 000 ]
}

case "${1:-}" in
  --dirs-only)
    dirs_ok && exit 0
    echo "UNSAFE $RUN is not $WANT" >&2; exit 13 ;;
  --base-ok)
    if tripped; then
      echo "TRIPPED the stream stopped itself; see $MARKER and the runbook" >&2; exit 11
    fi
    base_ok && exit 0
    echo "UNSAFE $RUN or $BASE is missing or not $WANT" >&2; exit 13 ;;
  --auth-set)
    auth_set && exit 0
    echo "AUTH $ETC/auth.env holds no password hash; the page would be open" >&2; exit 15 ;;
  --port-free)
    [ -z "$(listener)" ] && exit 0
    echo "PORT 127.0.0.1:$PORT is already taken" >&2; exit 14 ;;
  "") ;;
  *) echo "usage: check-local.sh [--dirs-only|--base-ok|--auth-set|--port-free]" >&2; exit 2 ;;
esac

found=()
code=0
severity() {
  case $1 in 11) echo 7;; 13) echo 6;; 14) echo 5;; 15) echo 4;; 16) echo 3;; 12) echo 2;;
             10) echo 1;; *) echo 0;; esac
}
note() {        # finding, exit code; the exit code kept is the most severe one
  found+=("$1")
  [ "$(severity "$2")" -gt "$(severity "$code")" ] && code=$2
  return 0
}

# Never print the marker's contents: it is only a sign that the stream stopped. A process
# that started just as the stream tripped is stopped here.
if tripped; then
  note "TRIPPED the stream stopped itself; see $MARKER and the runbook" 11
  for sc in rc-dashboard.scope rc-fleet-stream.scope; do
    systemctl --user is-active --quiet "$sc" && systemctl --user stop "$sc"
  done
fi
dirs_ok || note "UNSAFE $RUN or a folder in it is not $WANT" 13
auth_set || note "AUTH no password hash in $ETC/auth.env" 15

# Logs: the processes hold them open (appending), so copy and truncate.
for f in "$RUN"/logs/*.log; do
  [ -f "$f" ] && [ ! -L "$f" ] && [ "$(stat -c %s "$f")" -gt 104857600 ] && cp "$f" "$f.1" && : > "$f"
done

# Every listener on the port must be a process of the dashboard's own scope.
foreign=0
lines=$(listener)
if [ -n "$lines" ]; then
  mine=" $(scope_pids rc-dashboard.scope | tr '\n' ' ') "
  while IFS= read -r l; do
    [ -z "$l" ] && continue
    pid=$(grep -o 'pid=[0-9]*' <<< "$l" | head -1 | cut -d= -f2)
    { [ -n "$pid" ] && [[ "$mine" == *" $pid "* ]]; } || foreign=1
  done <<< "$lines"
  [ "$foreign" = 1 ] && note "PORT foreign listener on port $PORT" 14
fi

if [ "$code" != 11 ] && [ "$code" != 13 ]; then
  # Linger keeps the user manager, and with it the tmux session, running when no one
  # is logged in. An administrator or a cleanup script can turn it off (some sites do,
  # hourly, for accounts that run no tmux). This check's own login starts the user
  # manager, and with it the session, so the session may well be running now: linger
  # is what keeps it running once this login ends.
  if [ "$(loginctl show-user "$me" -p Linger --value 2>/dev/null)" != yes ]; then
    loginctl enable-linger "$me" 2>/dev/null || true
    note "LINGER was off; turned it on" 10
  fi
  if ! systemctl --user is-active --quiet "$UNIT"; then
    systemctl --user reset-failed "$UNIT" 2>/dev/null || true
    if systemctl --user start "$UNIT"; then
      note "RESTARTED $UNIT" 10
    else
      note "START FAILED $UNIT" 15
    fi
  else
    # A window whose loop ended (a start limit, or Ctrl-C) is started again; one that is
    # gone is made again. The loop runs in a window's first pane, whatever else someone
    # split off beside it. The dashboard's is left alone while another process has its port.
    for w in $WINDOWS; do
      [ "$w" = dashboard ] && [ "$foreign" = 1 ] && continue
      dead=$(tmx list-panes -t "rc-dashboard:$w" -F '#{pane_dead}' 2>/dev/null | head -1)
      [ "$dead" = 0 ] && continue
      if [ "$dead" = 1 ]; then
        tmx respawn-pane -t "rc-dashboard:$w.0"
      else
        tmx new-window -d -t rc-dashboard: -n "$w" "exec $KEEP $w"
      fi
      if [ $? -eq 0 ]; then note "RESTARTED $w" 10; else note "START FAILED $w" 15; fi
    done
    # A dashboard that listens must answer, if only with a 401, once it is 20 min old;
    # one that never listened gets 2 h. One that does not answer twice is stopped, and
    # its window's loop starts it again, unless it was replaced between the two tries.
    # A soft memory cap can stall it this way for an hour while every other check says OK.
    if [ "$foreign" = 0 ]; then
      pids=$(scope_pids rc-dashboard.scope)
      if [ -n "$pids" ]; then
        age=$(ps -o etimes= -p "$(paste -sd, <<< "$pids")" 2>/dev/null | sort -n | tail -1 | tr -d ' ')
        limit=$FROZEN_AFTER_S
        [ -z "$lines" ] && limit=$NEVER_LISTENED_S
        if [ "${age:-0}" -ge "$limit" ] && ! answers; then
          sleep "$RETRY_S"
          if ! answers && [ "$(scope_pids rc-dashboard.scope)" = "$pids" ]; then
            systemctl --user stop rc-dashboard.scope
            note "FROZEN the dashboard did not answer twice; restarted it" 10
          fi
        fi
      fi
    fi
  fi
fi

# Each source node's stream, from the receiver's status file (read only after the
# folder checks above passed).
if base_ok && [ -f "$BASE/state/status.json" ] && [ ! -L "$BASE/state/status.json" ]; then
  stream_active=0
  systemctl --user is-active --quiet rc-fleet-stream.scope && stream_active=1
  report=$(python3 - "$BASE/state/status.json" "$DOWN_ALERT_S" "$STALE_ALERT_S" "$stream_active" <<'PYEOF'
import json, sys, time
path, down_s, stale_s, active = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4] == "1"
try:
    st = json.load(open(path))
except (OSError, ValueError) as e:
    print("STREAM status unreadable: %s" % e)
    sys.exit(0)
now = time.time()
age = now - float(st.get("at_epoch") or 0)
if active and age > stale_s:
    print("STREAM status is %d min old" % (age // 60))
for host, s in sorted((st.get("hosts") or {}).items()):
    since = s.get("down_since")
    if not s.get("connected") and since and now - since > down_s:
        err = (s.get("last_error") or "no error recorded")[:160]
        print("STREAM %s down %.1f h: %s" % (host, (now - since) / 3600, err))
PYEOF
  )
  while IFS= read -r l; do
    [ -n "$l" ] && note "$l" 16
  done <<< "$report"
fi

# Privacy audit: nothing readable by group or others, nothing owned by anyone else,
# and no extended ACLs. Symlinks are skipped (their mode means nothing). Off with
# PRIVATE_TREE=false, where those are the user's business.
trees=()
for t in "$RUN" "$R"; do
  [ -d "$t" ] || continue
  [ ${#trees[@]} -gt 0 ] && [ "$t" = "${trees[0]}" ] && continue
  trees+=("$t")
done
if [ "$PRIVATE" = yes ] && [ ${#trees[@]} -gt 0 ]; then
  bad=$(find "${trees[@]}" ! -type l \( ! -user "$me" -o -perm /077 \) -print 2>/dev/null | wc -l)
  acl=$(getfacl -R -s -p "${trees[@]}" 2>/dev/null | grep -c '^# file:')
  [ "$((bad + acl))" -gt 0 ] && note "AUDIT $((bad + acl)) findings ($bad modes or owners, $acl ACLs)" 12
fi

if [ ${#found[@]} -eq 0 ]; then
  echo OK
else
  (IFS=';'; echo "${found[*]}")
fi
exit "$code"
