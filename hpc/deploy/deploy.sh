#!/bin/bash
# deploy.sh [--first] [--restart]: ship HEAD's dashboard and fleet stream to the
# dashboard's host, and the stream's sender to the source nodes. Run on this machine,
# from a tree with no uncommitted changes to tracked files.
#
# Builds the web bundle here, takes the runtime files from `git archive HEAD`, renders
# the tmux unit and the settings from site.env, and installs them on the dashboard's
# host (DASH_SSH) under DASH_ROOT/releases/<sha>, with `current` moved to it. The sender
# (hpc/fleet/fleet_stream.py) goes first, to SENDER_DIR on the nodes (ship-sender.sh); the
# stream key's forced command runs it there. Only code moves; no capture data passes
# through this machine. --first also makes the venv; --restart restarts the tmux
# session, and with it the stream and the dashboard (whose history then refills). It
# also installs check.sh and site.env into $WORK_DIR/bin for the hourly check.
#
# Needs here: git, python3, uv (to pin the dashboard's requirements), node and npm (the
# bundle), and ssh. Needs there: README.md, "What each host needs".
set -euo pipefail
umask 077
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
kit=${here#"$repo"/}                 # this kit's folder in the repository
stream=$(dirname "$kit")/fleet       # the fleet stream's: the kit's sibling, as keep-running.sh has it
# shellcheck source=/dev/null
. "$here/site.env"
# render.py checks the keys it renders; these two are this script's own, and SSH_KEY is
# first read after the sender has shipped, so an unset one stops here instead.
: "${WORK_DIR:?WORK_DIR is not set in site.env}" "${SSH_KEY:?SSH_KEY is not set in site.env}"
first=0 restart=0
for a in "$@"; do
  case $a in
    --first) first=1 ;;
    --restart) restart=1 ;;
    *) echo "usage: deploy.sh [--first] [--restart]" >&2; exit 2 ;;
  esac
done

cd "$repo"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "deploy.sh ships HEAD only; commit or set aside the changes first" >&2
  exit 1
fi
sha=$(git rev-parse --short=12 HEAD)
work=$WORK_DIR/build/$sha
rm -rf "$work"
mkdir -p "$work"

echo "==> render"
# Every template first: render.py checks the values used in the remote commands below
# (DASH_SSH, DASH_ROOT, DASH_PYTHON, SENDER_DIR, the nodes): set, and with no quotes,
# spaces, or backslashes. A bad site.env stops here, before anything is shipped.
for t in rc-tmux.service dashboard.config.json stream.json; do
  python3 "$here/render.py" "$here/$t.in" "$here/site.env" > "$work/$t"
done

echo "==> bundle"
if [ ! -e dashboard/web/node_modules ]; then
  if [ -n "${NODE_MODULES:-}" ]; then
    ln -s "$NODE_MODULES" dashboard/web/node_modules
  else
    (cd dashboard/web && npm ci --no-audit --no-fund >/dev/null)
  fi
fi
(cd dashboard/web && npx vite build --outDir "$work/dist" --emptyOutDir >/dev/null)

echo "==> release $sha"
git archive --format=tar --prefix=release/ HEAD dashboard/rc_dashboard dashboard/risk-profiles \
  dashboard/pyproject.toml dashboard/uv.lock "$stream" "$kit" > "$work/release.tar"
(cd dashboard && uv export --frozen --no-dev --no-emit-project --format requirements-txt \
  --quiet) > "$work/requirements.txt"
git show "HEAD:$kit/install-remote.sh" > "$work/install-remote.sh"

if [ -n "${SENDER_DIR:-}" ]; then
  echo "==> sender to $SENDER_DIR"
  tar -xOf "$work/release.tar" "release/$stream/fleet_stream.py" > "$work/fleet_stream.py"
  SITE_ENV="$here/site.env" "$here/ship-sender.sh" "$sha" "$work/fleet_stream.py"
fi

ssh_=(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20 -o LogLevel=ERROR)
echo "==> ship to $DASH_NODE"
tar -C "$work" -czf - dist release.tar requirements.txt rc-tmux.service dashboard.config.json \
  stream.json | "${ssh_[@]}" "$DASH_SSH" \
  "umask 077; mkdir -p $DASH_ROOT/incoming/$sha && tar -C $DASH_ROOT/incoming/$sha -xzf -"
# The script goes over stdin: run from a folder it deletes, NFS would keep the open
# file as a .nfs placeholder and the folder could not be removed.
"${ssh_[@]}" "$DASH_SSH" "bash -s -- $sha $first $restart $DASH_ROOT ${DASH_PYTHON:-python3}" \
  < "$work/install-remote.sh"

echo "==> hourly check in $WORK_DIR/bin"
mkdir -p "$WORK_DIR/bin"
install -m 700 "$here/check.sh" "$WORK_DIR/bin/check.sh"
install -m 600 "$here/site.env" "$WORK_DIR/bin/site.env"
rm -rf "$work"
echo "deployed $sha to $DASH_NODE"
