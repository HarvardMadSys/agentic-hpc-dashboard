#!/bin/bash
# check.sh: hourly, from this machine's crontab (README.md, "The hourly check"). Runs
# check-local.sh on the dashboard's host, in the deployment's folder (DASH_SSH and
# DASH_ROOT in site.env): it turns linger back on, starts the tmux session after a
# reboot, starts again a window whose loop ended, restarts a dashboard that stopped
# answering, and audits that nothing there is readable by anyone else. Anything but OK
# is posted to Slack when SLACK_WEBHOOK_FILE names a webhook; DRY_SLACK=1 prints the
# post instead. Either way the finding is in this script's log.
set -u
umask 077
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=/dev/null
. "${SITE_ENV:-$here/site.env}"
# This kit's folder in a release. deploy.sh installs this script by itself, beside a
# copy of site.env, so the path is written here; tests/test_render.py checks it.
KIT=deploy/fleet

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

for k in DASH_SSH DASH_ROOT; do
  if [ -z "${!k:-}" ]; then
    log "site.env sets no $k: nothing checked"
    exit 2
  fi
done

slack() {       # post $1; the webhook URL is a secret, so curl reads it from a pipe
  if [ "${DRY_SLACK:-0}" = 1 ]; then
    log "slack (dry): $1"
    return 0
  fi
  [ -n "${SLACK_WEBHOOK_FILE:-}" ] || return 0
  [ -s "$SLACK_WEBHOOK_FILE" ] || { log "slack: $SLACK_WEBHOOK_FILE is missing or empty"; return 1; }
  local url
  url=$(<"$SLACK_WEBHOOK_FILE"); url=${url//[[:space:]]/}
  if jq -n --arg text "$1" '{text: $text}' |
     curl -fsS -m 20 -H 'Content-Type: application/json' --data-binary @- -o /dev/null \
          -K <(printf 'url = "%s"\n' "$url"); then
    log "slack: posted"
  else
    log "slack: post failed"
  fi
}

out=$(timeout 600 ssh -i "${SSH_KEY:-$HOME/.ssh/id_ed25519}" -o IdentitiesOnly=yes -o BatchMode=yes \
        -o ConnectTimeout=20 -o ServerAliveInterval=30 -o LogLevel=ERROR "$DASH_SSH" \
        "RC_DASH_ROOT=$DASH_ROOT $DASH_ROOT/current/$KIT/check-local.sh" 2>&1)
rc=$?
log "rc=$rc ${out//$'\n'/ | }"
if [ "$rc" -ne 0 ]; then
  slack "*rc dashboard* on \`${DASH_NODE:-$DASH_SSH}\`: ${out:-no output} (exit $rc)"
fi
exit "$rc"
