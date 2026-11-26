#!/bin/bash
# check-local.sh: run on the dashboard's host, as the account that owns the deployment,
# with RC_DASH_ROOT set to the deployment's folder (rc-tmux.service and check.sh set it).
#
#   check-local.sh --dirs-only   the folder and its cache/, logs/, and data/ are real 700
#                                folders owned by this account (created if missing), and
#                                so are the copy's data/mirror and data/mirror/.state
#                                where they are (pull.py makes them); exit 0 or 13
#   check-local.sh --base-ok     the folder and its data/ (where the copy goes) are real
#                                700 folders owned by this account, and the copy's
#                                folders where they are; exit 0 or 13
#   check-local.sh --auth-set    etc/password.hash (or etc/auth.env) holds a password
#                                hash that parses; exit 0 or 15
#   check-local.sh --port-free   nothing listens on the dashboard's port yet; exit 0 or 14
#                                (14 too when it cannot tell)
#   check-local.sh               the hourly check (check.sh runs it over ssh): all of the
#                                above, linger, the tmux session and its two windows (a
#                                window whose loop ended is started again), a dashboard
#                                that stopped answering, each node's copy, the
#                                dashboard's own verdict (rc-dashboard status), log
#                                rotation, and the privacy audit. One line out.
#
# keep-running.sh also runs the start checks before each start of its process.
#
# With site.env's PRIVATE_TREE=false (etc/kit.env), "a 700 folder owned by this account"
# below reads "a real folder", and there is no privacy audit.
#
# Exit codes, most severe first when several apply: 13 UNSAFE (a folder is not this
# account's 700 folder), 14 PORT (someone else listens on the port, or who listens cannot
# be told), 15 AUTH (no password hash, one that does not parse, or a start failed), 16
# COPY (a node's copy failing for over an hour, never good, or stale while the copy
# runs), 17 STATUS (rc-dashboard status says warn or crit: a
# collector silent, stopped or degraded), 12 AUDIT (files readable by others, or
# extended ACLs), 10 RESTARTED, FROZEN, or LINGER
# (something was started or restarted, or linger was turned back on), 0 OK; 2 for a
# usage error, or no RC_DASH_ROOT.
set -u
umask 077
me=$(id -un)
uid=$(id -u)
# Everything lives in the deployment's folder; there is no default. The tests point the
# run folder (data, cache, logs), the copy's base, and etc/ elsewhere; the session never
# sets those.
R=${RC_DASH_ROOT:-}
if [ -z "$R" ]; then
  echo "check-local.sh: RC_DASH_ROOT is not set; it names the deployment's folder" >&2
  exit 2
fi
RUN=${RC_DASH_RUN:-$R}
BASE=${RC_FLEET_BASE:-$RUN/data}
ETC=${RC_FLEET_ETC:-$R/etc}
# The tmux session that runs both processes, its windows (tmux-session.sh), and the
# loop each window runs.
UNIT=rc-tmux.service
WINDOWS="copy dashboard"
KEEP=$(dirname "$0")/keep-running.sh
CGROUP_ROOT=${RC_CGROUP_ROOT:-/sys/fs/cgroup}
PROC_NET=${RC_PROC_NET:-/proc/net}
DOWN_ALERT_S=3600          # a node's copy failing, or never good, this long is reported
FROZEN_AFTER_S=1200        # a listening dashboard this old must answer
NEVER_LISTENED_S=7200      # one not listening yet gets this long: a start on a busy core is slow
RETRY_S=${RC_CHECK_RETRY_S:-30}
# The dashboard's port, from its settings; the dashboard's own default without them.
PORT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["server"]["port"])' \
       "$ETC/dashboard.config.json" 2>/dev/null || echo 8080)
# site.env's PRIVATE_TREE, as deploy.sh renders it into etc/kit.env. "no" (a host that only
# the team can log in to): modes, owners, and ACLs are the user's business, so the folders
# must only be real folders, and the privacy audit is off. Anything unreadable counts as
# "yes".
PRIVATE=$(sed -n 's/^PRIVATE_TREE=//p' "$ETC/kit.env" 2>/dev/null | tail -n 1)
[ "$PRIVATE" = no ] || PRIVATE=yes
if [ "$PRIVATE" = no ]; then
  WANT="a real folder"
else
  WANT="a 700 folder owned by $me"
fi

judged=         # the folder judged last: after a failed check, the one that failed
private_dir() {  # a real folder (not a symlink); unless PRIVATE=no, this account's, mode 700
  judged=$1
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
# The copy's folder and its state (fleet.env's FLEET_MIRROR, under data/), which pull.py
# makes as PRIVATE_TREE says, so they are not made here: where they are, they are judged
# as data/ is. pull.py takes a link there for the folder, so the copy, and the chmod it
# gives a private one, would land outside the deployment's folder.
copy_ok() {
  local d
  for d in "$BASE/mirror" "$BASE/mirror/.state"; do
    if [ -e "$d" ] || [ -L "$d" ]; then private_dir "$d" || return 1; fi
  done
}
dirs_ok() { own_dir "$RUN" && own_dir "$RUN/cache" && own_dir "$RUN/logs" && own_dir "$BASE" && copy_ok; }
base_ok() { private_dir "$RUN" && private_dir "$BASE" && copy_ok; }
unquote() {     # as keep-running.sh, and systemd, read a value: blanks, then one pair of quotes off
  local v=$1
  v=${v#"${v%%[![:space:]]*}"}
  v=${v%"${v##*[![:space:]]}"}
  if [ ${#v} -ge 2 ]; then
    case $v in \"*\"|\'*\') v=${v:1:${#v}-2} ;; esac
  fi
  printf '%s' "$v"
}
# The password's hash as the dashboard will see it, and where it came from: the hash
# file (etc/password.hash, or RC_DASH_PASSWORD_HASH_FILE in auth.env), else
# RC_DASH_PASSWORD_HASH in auth.env. Sets AUTH_FROM and AUTH_HASH.
auth_read() {
  local line file=
  AUTH_FROM= AUTH_HASH=
  if [ -f "$ETC/auth.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case $line in
        RC_DASH_PASSWORD_HASH_FILE=*) file=$(unquote "${line#*=}") ;;
        RC_DASH_PASSWORD_HASH=*) AUTH_HASH=$(unquote "${line#*=}"); AUTH_FROM=$ETC/auth.env ;;
      esac
    done < "$ETC/auth.env"
  fi
  [ -n "$file" ] || { [ -s "$ETC/password.hash" ] && file=$ETC/password.hash; }
  if [ -n "$file" ]; then
    # As the dashboard reads it (auth.read_hash_file): blank lines and `#` lines passed
    # over, the blanks and one pair of quotes around the hash taken off. Two lines are
    # no hash: joined, they fail to parse, as the dashboard refuses them.
    AUTH_FROM=$file AUTH_HASH=
    if [ -f "$file" ]; then
      while IFS= read -r line || [ -n "$line" ]; do
        line=${line#"${line%%[![:space:]]*}"}
        case $line in ''|\#*) continue ;; esac
        AUTH_HASH=${AUTH_HASH:+$AUTH_HASH }$(unquote "$line")
      done < "$file"
    fi
  fi
}
# Whether a hash parses, and why not: the dashboard's own parser where the venv is
# (rc_dashboard.auth.parse_hash), else its shape, scrypt$n$r$p$salt$key. The hash goes
# over stdin, never on a command line.
HASH_RE='^scrypt[$][0-9]+[$][0-9]+[$][0-9]+[$][A-Za-z0-9+/]+=*[$][A-Za-z0-9+/]+=*$'
hash_why() {
  local why
  if [ -x "$R/venv/bin/python" ] && [ -d "$R/current/dashboard" ]; then
    why=$(cd "$R/current/dashboard" && printf '%s' "$1" | "$R/venv/bin/python" -c '
import sys
try:
    from rc_dashboard.auth import parse_hash
except Exception:
    sys.exit(3)
try:
    parse_hash(sys.stdin.read())
except ValueError as e:
    print(e)
    sys.exit(1)' 2>/dev/null)
    case $? in 0) return 0 ;; 1) printf '%s' "$why"; return 1 ;; esac
  fi
  [[ $1 =~ $HASH_RE ]] && return 0
  printf 'not a scrypt$n$r$p$salt$key hash'
  return 1
}
AUTH_WHY=
auth_set() {
  local why
  auth_read
  if [ -z "$AUTH_HASH" ]; then
    AUTH_WHY="no password hash in $ETC/password.hash or $ETC/auth.env; the page would be open"
    return 1
  fi
  if ! why=$(hash_why "$AUTH_HASH"); then
    AUTH_WHY="the password hash in $AUTH_FROM does not parse ($why): the dashboard would not start. Make one with rc-deploy password, or hpc/deploy/password.sh"
    return 1
  fi
  return 0
}
# Every TCP listener on the port, one line each, with pid=<n> where the process is this
# account's. From ss; without ss, or when it fails, from /proc/net/tcp and tcp6, which
# list every account's sockets as ss does (lsof, run as this account, would show none of
# another account's). Fails, printing nothing, when neither can be read: who listens is
# then unknown, and no caller takes that for "nobody".
listener() {
  local out
  if command -v ss >/dev/null 2>&1 && out=$(ss -ltnpH "sport = :$PORT" 2>/dev/null); then
    [ -z "$out" ] || printf '%s\n' "$out"
    return 0
  fi
  python3 - "$PORT" "$PROC_NET" 2>/dev/null <<'PYEOF'
import errno, os, sys
port, net = int(sys.argv[1]), sys.argv[2]
inodes = {}
for name in ("tcp", "tcp6"):
    try:
        with open(os.path.join(net, name)) as fh:
            rows = fh.read().splitlines()[1:]
    except OSError as e:
        if name == "tcp6" and e.errno == errno.ENOENT:
            continue                    # no IPv6 here, so no IPv6 listener
        sys.exit(1)
    for row in rows:                    # sl local remote st queues tr retr uid timeout inode
        f = row.split()
        if len(f) > 9 and f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
            inodes[f[9]] = f[1]         # 0A: LISTEN
pids = {}
for pid in (os.listdir("/proc") if inodes else []):
    if not pid.isdigit():
        continue
    try:
        fds = os.listdir("/proc/%s/fd" % pid)
    except OSError:
        continue                        # another account's process, or gone
    for fd in fds:
        try:
            link = os.readlink("/proc/%s/fd/%s" % (pid, fd))
        except OSError:
            continue
        if link.startswith("socket:[") and link[8:-1] in inodes:
            pids.setdefault(link[8:-1], set()).add(int(pid))
for inode, local in sorted(inodes.items()):
    print(" ".join(["LISTEN", local, "inode=" + inode]
                   + ["pid=%d" % p for p in sorted(pids.get(inode, ()))]))
PYEOF
}
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
    echo "UNSAFE ${judged:-$RUN} is not $WANT" >&2; exit 13 ;;
  --base-ok)
    base_ok && exit 0
    echo "UNSAFE ${judged:-$RUN} is missing or not $WANT" >&2; exit 13 ;;
  --auth-set)
    auth_set && exit 0
    echo "AUTH $AUTH_WHY" >&2; exit 15 ;;
  --port-free)
    if ! lines=$(listener); then
      echo "PORT cannot tell whether port $PORT is free: ss is missing or failed, and $PROC_NET/tcp could not be read" >&2; exit 14
    fi
    [ -z "$lines" ] && exit 0
    echo "PORT 127.0.0.1:$PORT is already taken" >&2; exit 14 ;;
  "") ;;
  *) echo "usage: check-local.sh [--dirs-only|--base-ok|--auth-set|--port-free]" >&2; exit 2 ;;
esac

found=()
code=0
severity() {
  case $1 in 13) echo 6;; 14) echo 5;; 15) echo 4;; 16|17) echo 3;; 12) echo 2;;
             10) echo 1;; *) echo 0;; esac
}
note() {        # finding, exit code; the exit code kept is the most severe one
  found+=("$1")
  [ "$(severity "$2")" -gt "$(severity "$code")" ] && code=$2
  return 0
}

dirs_ok || note "UNSAFE ${judged:-$RUN} is not $WANT" 13
auth_set || note "AUTH $AUTH_WHY" 15

# Logs: the processes hold them open (appending), so copy and truncate.
for f in "$RUN"/logs/*.log; do
  [ -f "$f" ] && [ ! -L "$f" ] && [ "$(stat -c %s "$f")" -gt 104857600 ] && cp "$f" "$f.1" && : > "$f"
done

# Every listener on the port must be a process of the dashboard's own scope. When the
# listeners cannot be listed, that is reported, and the dashboard is left alone as it is
# beside a foreign listener.
foreign=0
if ! lines=$(listener); then
  note "PORT cannot tell who listens on port $PORT: ss is missing or failed, and $PROC_NET/tcp could not be read" 14
  foreign=1
elif [ -n "$lines" ]; then
  mine=" $(scope_pids rc-dashboard.scope | tr '\n' ' ') "
  while IFS= read -r l; do
    [ -z "$l" ] && continue
    pid=$(grep -o 'pid=[0-9]*' <<< "$l" | head -1 | cut -d= -f2)
    { [ -n "$pid" ] && [[ "$mine" == *" $pid "* ]]; } || foreign=1
  done <<< "$lines"
  [ "$foreign" = 1 ] && note "PORT foreign listener on port $PORT" 14
fi

if [ "$code" != 13 ]; then
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

# Each node's copy, from pull.py's state (read only after the folder checks above passed):
# a node whose passes have failed for over an hour, one never copied well, or one with no
# good pass for an hour (pull.py status's STALE, which a stopped copy leaves).
# RC_PULL_CMD stands in for the command (tests).
if base_ok && [ -d "$BASE/mirror/.state" ] && [ ! -L "$BASE/mirror/.state" ]; then
  if [ -n "${RC_PULL_CMD:-}" ]; then
    pulled=$(bash -c "$RC_PULL_CMD" 2>/dev/null)
  else
    pulled=$(timeout 60 /usr/bin/python3 "$R/current/deploy/fleet/pull.py" --config "$ETC/fleet.env" \
             status --stale "$DOWN_ALERT_S" --json 2>/dev/null)
  fi
  report=$(printf '%s' "$pulled" | python3 -c '
import json, sys
down_s = float(sys.argv[1])
try:
    doc = json.loads(sys.stdin.read())
    rows = doc["nodes"]
except (ValueError, KeyError, TypeError):
    print("COPY its state could not be read (pull.py status)")
    sys.exit(0)
for r in rows:
    state, age = r.get("state"), r.get("last_ok_age_s")
    err = str(r.get("error") or "no error recorded")[:160]
    if state == "never" or (state in ("FAILED", "STALE") and age is None):
        print("COPY %s never copied: %s" % (r.get("host"), err))
    elif state == "STALE" or (state == "FAILED" and age > down_s):
        print("COPY %s %s for %.1f h: %s" % (r.get("host"), state, age / 3600, err))
' "$DOWN_ALERT_S" 2>/dev/null)
  while IFS= read -r l; do
    [ -n "$l" ] && note "$l" 16
  done <<< "$report"
fi

# The dashboard's own verdict on every collector and link, from the settings it serves
# with: `rc-dashboard status --json` (a heartbeat gone quiet, a probe that could not
# load, a node's copy that fails). A level of warn or crit is a finding.
# RC_STATUS_CMD stands in for the command (tests).
status_findings() {
  local out err rc
  err=$(mktemp "$RUN/.status-err.XXXXXX" 2>/dev/null) || return 0
  if [ -n "${RC_STATUS_CMD:-}" ]; then
    out=$(bash -c "$RC_STATUS_CMD" 2>"$err"); rc=$?
  elif [ -x "$R/venv/bin/python" ] && [ -d "$R/current/dashboard" ]; then
    out=$(cd "$R/current/dashboard" && RC_DASH_CONFIG=$ETC/dashboard.config.json \
          RC_DASH_TRANSPORT_FILES=${RC_DASH_TRANSPORT_FILES:-$BASE/mirror/.state/*.json} \
          timeout 300 "$R/venv/bin/python" -m rc_dashboard status --json 2>"$err"); rc=$?
  else
    rm -f "$err"
    return 0                      # no release installed yet
  fi
  if grep -q "invalid choice: 'status'" "$err" 2>/dev/null; then
    rm -f "$err"
    return 0                      # a release from before `status`
  fi
  printf '%s' "$out" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except ValueError:
    sys.exit(4)
level = d.get("level") if isinstance(d, dict) else None
if level not in ("ok", "warn", "crit"):
    sys.exit(4)
if level != "ok":
    why = "; ".join(str(w) for w in (d.get("why") or [])) or "no reason given"
    print("STATUS %s: %s" % (level, why[:400]))
' 2>/dev/null
  if [ "${PIPESTATUS[1]}" = 4 ]; then
    echo "STATUS rc-dashboard status gave no verdict (exit $rc): $(tail -n 1 "$err" 2>/dev/null | cut -c1-200)"
  fi
  rm -f "$err"
}
if [ "$code" != 13 ]; then
  while IFS= read -r l; do
    [ -n "$l" ] && note "$l" 17
  done <<< "$(status_findings)"
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
