#!/bin/bash
# check.sh [--print] [--help]: the hourly check that rc-dashboard-check.timer runs, as the
# dashboard's account and with its settings (dashboard.env, paths.env), on the dashboard's
# host: `rc-dashboard status --json`, which judges every collector from its heartbeats and,
# in a fleet, every node's copy (RC_DASH_TRANSPORT_FILES, pull.py's state files). A level of
# warn or crit is logged (journalctl -u rc-dashboard-check), and posted to Slack when
# ETC/slack_webhook holds a webhook URL (mode 640, root:rc-dashboard; never printed).
# DRY_SLACK=1 prints the post instead. In a fleet it also reads each node's copy
# (pull.py status): a node FAILED, never pulled, or STALE (rc-pull not running) is crit,
# whatever the dashboard has read. Exit as status does: 0 ok, 1 warn, 2 crit; 3 when the
# status could not be had at all.
#
#   --print   for a person, as root or as the dashboard's account: the status as the
#             dashboard prints it, and in a fleet each node's copy (pull.py status).
#             deploy/rc-deploy status runs this.
#
# Run as root, it runs itself again as the dashboard's account (SERVICE_USER in
# /etc/rc-dashboard/install.env), so nothing it writes is root's. RC_STATUS_CMD stands in
# for the status command (tests).
set -u
umask 077
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
INSTALL_ENV=${INSTALL_ENV:-/etc/rc-dashboard/install.env}

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
say() { printf '==> %s\n' "$*"; }
die() { printf 'check.sh: %s\n' "$*" >&2; exit 3; }
# shellcheck source=SCRIPTDIR/lib.sh
. "$here/lib.sh"

print=0
for a in "$@"; do
  case $a in
    --print) print=1 ;;
    -h|--help) show_help "$0"; exit 0 ;;
    *) echo "usage: check.sh [--print] [--help]" >&2; exit 2 ;;
  esac
done

install_env_load SERVICE_USER ETC OPT MODE
SERVICE_USER=${SERVICE_USER:-rc-dashboard}
ETC=${RC_CHECK_ETC:-${ETC:-/etc/rc-dashboard}}
VENV=${RC_CHECK_VENV:-${OPT:-/opt/rc-dashboard}/venv}
MODE=${MODE:-single}

if [ "$(id -u)" = 0 ] && [ -z "${RC_CHECK_AGAIN:-}" ]; then
  id "$SERVICE_USER" >/dev/null 2>&1 || die "no account $SERVICE_USER: is the dashboard installed here?"
  pass=(RC_CHECK_AGAIN=1 "RC_CHECK_ETC=$ETC" "RC_CHECK_VENV=$VENV" "INSTALL_ENV=$INSTALL_ENV")
  [ -n "${DRY_SLACK:-}" ] && pass+=("DRY_SLACK=$DRY_SLACK")
  [ -n "${RC_STATUS_CMD:-}" ] && pass+=("RC_STATUS_CMD=$RC_STATUS_CMD")
  exec runuser -u "$SERVICE_USER" -- env "${pass[@]}" bash "$0" "$@"
fi

# The dashboard's settings, as systemd gives them to the unit (by hand they are not in
# the environment yet): KEY=VALUE, the blanks and one pair of quotes around it dropped.
load_env() {
  local line key
  [ -r "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in
      RC_DASH_*=*) key=${line%%=*}; export "$key=$(env_value "$1" "$key")" ;;
    esac
  done < "$1"
}
load_env "$ETC/dashboard.env"
load_env "$ETC/paths.env"

status() {      # ARGS...: the dashboard's status command
  if [ -n "${RC_STATUS_CMD:-}" ]; then
    bash -c "$RC_STATUS_CMD" -- "$@"
  else
    (cd "$repo/dashboard" && "$VENV/bin/python" -m rc_dashboard status "$@")
  fi
}

if [ "$print" = 1 ]; then
  status
  rc=$?
  if [ "$MODE" = fleet ] && [ -e "$ETC/fleet.env" ]; then
    echo
    echo "the copy of each node (pull.py status):"
    "$VENV/bin/python" "$repo/deploy/fleet/pull.py" --config "$ETC/fleet.env" status || true
  fi
  exit "$rc"
fi

slack() {       # post $1; the webhook URL is a secret, so curl reads it from a pipe
  if [ "${DRY_SLACK:-0}" = 1 ]; then
    log "slack (dry): $1"
    return 0
  fi
  local f=$ETC/slack_webhook url
  [ -s "$f" ] || return 0
  url=$(<"$f"); url=${url//[[:space:]]/}
  if "$VENV/bin/python" -c 'import json, sys; print(json.dumps({"text": sys.argv[1]}))' "$1" |
     curl -fsS -m 20 -H 'Content-Type: application/json' --data-binary @- -o /dev/null \
          -K <(printf 'url = "%s"\n' "$url"); then
    log "slack: posted"
  else
    log "slack: post failed"
  fi
}

err=$(mktemp)
out=$(status --json 2>"$err")
rc=$?
if grep -q "invalid choice: 'status'" "$err" 2>/dev/null; then
  rm -f "$err"
  log "this release has no rc-dashboard status: nothing checked"
  exit 0
fi
# In a fleet, the copy itself too: a node FAILED, never pulled, or STALE (rc-pull is not
# running) is crit, whatever the dashboard has read so far.
copy=
if [ "$MODE" = fleet ] && [ -e "$ETC/fleet.env" ]; then
  copy=$("$VENV/bin/python" "$repo/deploy/fleet/pull.py" --config "$ETC/fleet.env" status --json 2>/dev/null \
    | "$VENV/bin/python" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
bad = ["%s %s (%s)" % (r["host"], r["state"], r.get("error") or "") for r in d.get("nodes", []) if not r.get("ok")]
if bad:
    print("copy: " + "; ".join(bad))
' 2>/dev/null)
fi
summary=$(printf '%s' "$out" | "$VENV/bin/python" -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    level = d["level"]
except (ValueError, KeyError, TypeError):
    sys.exit(4)
parts = [str(w) for w in (d.get("why") or [])]
for h in d.get("hosts") or []:
    t = h.get("transport") or {}
    bad = h.get("verdict") not in ("ok", None) or t.get("ok") is False
    if bad:
        parts.append("%s: %s%s" % (h.get("host"), h.get("verdict"),
                                   (" (copy: %s)" % t.get("error")) if t.get("ok") is False else ""))
print("%s %s" % (level, "; ".join(dict.fromkeys(parts))[:1500]))
' 2>/dev/null)
parsed=$?
host=$(hostname -s 2>/dev/null || hostname)
if [ "$parsed" != 0 ]; then
  why=$(tail -n 1 "$err" 2>/dev/null | cut -c1-300)
  rm -f "$err"
  log "status could not be had (exit $rc): ${why:-no output}"
  slack "*rc dashboard* on \`$host\`: the hourly check could not run rc-dashboard status (exit $rc): ${why:-no output}"
  exit 3
fi
rm -f "$err"
level=${summary%% *}
detail=${summary#* }
if [ -n "$copy" ]; then
  level=crit
  if [ -n "$detail" ] && [ "$detail" != "$summary" ]; then detail="$detail; $copy"; else detail=$copy; fi
fi
if [ "$level" = ok ]; then
  log "ok"
  exit 0
fi
log "$level: $detail"
slack "*rc dashboard* on \`$host\`: $level: $detail"
case $level in warn) exit 1 ;; *) exit 2 ;; esac
