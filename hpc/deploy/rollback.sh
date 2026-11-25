#!/bin/bash
# rollback.sh [SHA] [--plan] [--help]: the dashboard's host back to the release it ran
# before (or to SHA, one of DASH_ROOT/releases), from this machine. Run on this machine,
# with site.env filled in.
#
# On the dashboard's host, `current` moves to that release, its pinned requirements go
# into the venv, and the session restarts (the dashboard refills its history). Each
# install-remote.sh notes its release in DASH_ROOT/releases/.history, which says which one
# came before. The settings stay as the last deploy rendered them, but for a release that
# reads no pattern root, whose settings name each node's folder in place of the copy's
# pattern, and for one from before the password's hash file, whose etc/auth.env holds the
# hash in etc/password.hash again. On the nodes, SENDER_DIR/current goes back to that
# release's code too, where a node still has it (install-sender.sh keeps the one before):
# a release from before the copy (deploy/fleet/pull.py) streamed with a sender of its own,
# which the copy's guard does not answer. To go back for good, check out the older commit
# and run deploy.sh --restart.
# --plan prints the release it would go back to, and changes nothing.
set -euo pipefail
umask 077
here=$(cd "$(dirname "$0")" && pwd)
want= plan=0
for a in "$@"; do
  case $a in
    --plan) plan=1 ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    -*) echo "usage: rollback.sh [SHA] [--plan] [--help]" >&2; exit 2 ;;
    *) [[ $a =~ ^[0-9a-f]{12}$ ]] || { echo "rollback.sh: $a is not a release (12 hex digits)" >&2; exit 2; }
       want=$a ;;
  esac
done
# shellcheck source=/dev/null
. "${SITE_ENV:-$here/site.env}"
: "${DASH_SSH:?DASH_SSH is not set in site.env}" "${DASH_ROOT:?DASH_ROOT is not set in site.env}"
ssh_=(ssh -i "${SSH_KEY:-$HOME/.ssh/id_ed25519}" -o IdentitiesOnly=yes -o BatchMode=yes
      -o ConnectTimeout=20 -o LogLevel=ERROR)
# The host first; the remote script's last line names the release it went back to.
out=$("${ssh_[@]}" "$DASH_SSH" "bash -s -- $DASH_ROOT '$want' $plan" <<'REMOTE'
set -euo pipefail
R=$1 want=$2 plan=$3
cur=$(readlink "$R/current" 2>/dev/null | sed 's#^releases/##')
if [ -z "$want" ]; then
  # the release installed before the current one, newest first
  want=$( (tac "$R/releases/.history" 2>/dev/null || true) | awk '{print $1}' \
          | grep -vx "$cur" | head -n 1 || true)
fi
if [ -z "$want" ] || [ ! -e "$R/releases/$want/.installed" ]; then
  echo "rollback.sh: no release to go back to (current: ${cur:-none}); DASH_ROOT/releases holds:" >&2
  ls -1 "$R/releases" >&2 || true
  exit 1
fi
if [ "$want" = "$cur" ]; then
  echo "release $want is current already"
  exit 0
fi
if [ "$plan" = 1 ]; then
  echo "would go back from ${cur:-none} to $want, and restart the session"
  exit 0
fi
"$R/venv/bin/python" -m pip install --quiet --require-hashes -r "$R/releases/$want/requirements.txt"
# A release from before the pattern root (config.expand_root) reads the copy's
# `data/mirror/*` as one folder of that name, which is none, and would start with no
# feed: its settings name each node's folder instead, as the pattern matches them now.
# The next deploy.sh renders the pattern again.
if ! grep -qs '^def expand_root' "$R/releases/$want/dashboard/rc_dashboard/config.py"; then
  py=$(command -v python3 || echo "$R/venv/bin/python")
  "$py" - "$R/etc/dashboard.config.json" <<'PY'
import glob, json, os, sys
p = sys.argv[1]
try:
    with open(p) as fh:
        cfg = json.load(fh)
except (OSError, ValueError):
    sys.exit(0)
feeds = cfg.get("feeds") or {}
ebpf = feeds.get("ebpf") or {}
roots = ebpf.get("roots") or []
out = []
for r in roots:
    if any(c in r for c in "*?["):
        out += sorted(m for m in glob.glob(r) if os.path.isdir(m))
    else:
        out.append(r)
if out != roots:
    ebpf["roots"] = out
    feeds["//ebpf"] = ("Each node's folder, written by rollback.sh for a release that reads no "
                       "pattern: the next deploy.sh renders the pattern again.")
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=1)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.rename(tmp, p)
    print("that release reads no pattern root: the settings name each node's folder (%d) "
          "until the next deploy.sh" % len(out))
PY
fi
# A release from before the hash file reads the password from etc/auth.env alone (its
# keep-running.sh, and its start check: RC_DASH_PASSWORD_HASH there), where password.sh
# has commented it out for etc/password.hash: it would not start. auth.env gets the hash
# in that file again, on a line of its own; a later release reads the file first.
if [ -s "$R/etc/password.hash" ] && \
   ! grep -qs 'password\.hash' "$R/releases/$want/hpc/deploy/keep-running.sh" && \
   ! grep -qs '^RC_DASH_PASSWORD_HASH=..' "$R/etc/auth.env"; then
  h=$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$R/etc/password.hash" \
      | grep -v -e '^#' -e '^$' | head -n 1 | tr -d "\"'")
  case $h in
    scrypt\$*)
      {
        if [ -s "$R/etc/auth.env" ]; then
          cat "$R/etc/auth.env"
          [ -z "$(tail -c 1 "$R/etc/auth.env")" ] || echo
        fi
        printf 'RC_DASH_PASSWORD_HASH=%s\n' "$h"
      } > "$R/etc/.auth.env.new"
      chmod 600 "$R/etc/.auth.env.new"
      mv -f "$R/etc/.auth.env.new" "$R/etc/auth.env"
      echo "that release reads no password.hash: etc/auth.env holds its hash again" ;;
  esac
fi
ln -sfn "releases/$want" "$R/current.new"
mv -T "$R/current.new" "$R/current"
printf '%s %s rollback\n' "$want" "$(date '+%F %T')" >> "$R/releases/.history"
systemctl --user restart rc-tmux.service
echo "current: $want (was ${cur:-none}); the session restarted"
echo "ROLLED_BACK_TO=$want"
REMOTE
) || { rc=$?; printf '%s\n' "$out"; exit "$rc"; }
printf '%s\n' "$out" | grep -v '^ROLLED_BACK_TO='
to=$(printf '%s\n' "$out" | sed -n 's/^ROLLED_BACK_TO=//p' | tail -n 1)
# The nodes: SENDER_DIR/current back to that release's code, where a node still has it
# (install-sender.sh keeps the one it replaced). Once with a shared home.
if [ -n "$to" ] && [ -n "${SENDER_DIR:-}" ]; then
  for node in $SOURCE_NODES; do
    dest="$SOURCE_USER@$node${SOURCE_DOMAIN:+.$SOURCE_DOMAIN}"
    if r=$("${ssh_[@]}" "$dest" "if [ -d $SENDER_DIR/$to ]; then ln -sfn $to $SENDER_DIR/current.new && mv -T $SENDER_DIR/current.new $SENDER_DIR/current && if [ -e $SENDER_DIR/$to/rsync_guard.py ]; then echo moved; else echo moved-stream; fi; else echo none; fi" 2>&1); then
      case $r in
        moved) echo "  $node: $SENDER_DIR/current is $to again" ;;
        # A release from before the copy has the stream's sender alone: a key line made
        # for the copy (rsync_guard.py) is not answered until it runs that sender again.
        moved-stream) echo "  $node: $SENDER_DIR/current is $to again, the stream's sender from before the copy: a key line that runs rsync_guard.py is not answered now; make it run fleet_stream.py send --root <SOURCE_ROOT> (hpc/deploy/README.md)" ;;
        *) echo "  $node: no $to in $SENDER_DIR; it keeps the code it has" ;;
      esac
      [ "${SHARED_HOME:-yes}" = yes ] && break
    else
      echo "  $node: not reached: $r" >&2
    fi
  done
fi
