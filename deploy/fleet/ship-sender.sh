#!/bin/bash
# ship-sender.sh SHA FILE: put FILE (a release's fleet/fleet_stream.py) on the source
# nodes as SENDER_DIR/SHA/fleet_stream.py, and move SENDER_DIR/current to it, with
# install-sender.sh run on each node. deploy.sh runs it before it ships the release, so
# the nodes never run an older sender than the receiver expects.
#
# SHARED_HOME=yes (the default): the nodes share one home, so the first node that
# answers is enough; a node that does not answer is skipped. SHARED_HOME=no: every node
# gets the sender, and a node that fails fails the step. Exit 0, 1 (a node failed, or
# none answered), or 2 (a usage error, or no SENDER_DIR).
set -uo pipefail
umask 077
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=/dev/null
. "${SITE_ENV:-$here/site.env}"
if [ $# -ne 2 ] || [ -z "$1" ] || [ ! -s "$2" ]; then
  echo "usage: ship-sender.sh SHA FILE" >&2
  exit 2
fi
sha=$1 file=$2
if [ -z "${SENDER_DIR:-}" ]; then
  echo "ship-sender.sh: site.env sets no SENDER_DIR; the nodes are left alone" >&2
  exit 2
fi
shared=${SHARED_HOME:-yes}
ssh_=(ssh -i "${SSH_KEY:-$HOME/.ssh/id_ed25519}" -o IdentitiesOnly=yes -o BatchMode=yes
      -o ConnectTimeout=20 -o LogLevel=ERROR)
done_=0 failed=()
for node in $SOURCE_NODES; do
  dest="$SOURCE_USER@$node${SOURCE_DOMAIN:+.$SOURCE_DOMAIN}"
  if "${ssh_[@]}" "$dest" "umask 077; mkdir -p $SENDER_DIR && chmod 700 $SENDER_DIR && cat > $SENDER_DIR/.incoming-$sha" \
       < "$file" &&
     "${ssh_[@]}" "$dest" "bash -s -- $sha $SENDER_DIR" < "$here/install-sender.sh"; then
    echo "  $node: sender $sha"
    done_=$((done_ + 1))
    [ "$shared" = yes ] && break
  else
    echo "  $node: FAILED" >&2
    failed+=("$node")
  fi
done
if [ "$done_" -eq 0 ] || { [ "$shared" = no ] && [ ${#failed[@]} -gt 0 ]; }; then
  echo "ship-sender.sh: the sender is not on ${failed[*]:-any node}" >&2
  exit 1
fi
exit 0
