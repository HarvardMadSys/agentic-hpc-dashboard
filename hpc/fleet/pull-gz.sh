#!/bin/bash
# pull-gz.sh PULL: copy every source node's sealed days, as the collector's own
# `.jsonl.gz`, into PULL/<host>/<day>/, the layout seed_mirror.py reads. Only
# `.jsonl.gz` files cross: the live day's plain files are left for the stream. Run it
# again to fetch only what is new; rsync skips the rest.
#
#   SOURCE_USER=<account> SOURCE_NODES="<node> ..." hpc/fleet/pull-gz.sh PULL
#   python3 hpc/fleet/seed_mirror.py PULL BASE     # then build the receiver's copy in BASE
#
# The names are the deploy kit's (hpc/deploy/site.env), so this works too:
#   (set -a; . hpc/deploy/site.env; hpc/fleet/pull-gz.sh PULL)
#
# The files name real users and show their commands: PULL is made 700, files 600.
# From the environment: SOURCE_USER and SOURCE_NODES (required), SOURCE_DOMAIN (each
# node is <node>.<SOURCE_DOMAIN>; empty: the node's name as it is), SOURCE_ROOT (the
# collector's folder, /scratch/ebpfm), and SSH_KEY (~/.ssh/id_ed25519).
# Exit 0; 1 if a node failed (the others are still pulled); 2 for a usage error.
set -uo pipefail
umask 077
if [ $# -ne 1 ]; then
  echo "usage: SOURCE_USER=<account> SOURCE_NODES=\"<node> ...\" pull-gz.sh PULL" >&2
  exit 2
fi
DEST=$1
if [ -z "${SOURCE_USER:-}" ] || [ -z "${SOURCE_NODES:-}" ]; then
  echo "set SOURCE_USER to the account that logs in to the nodes, and SOURCE_NODES to the nodes" >&2
  exit 2
fi
read -ra HOSTS <<< "$SOURCE_NODES"
DOMAIN=${SOURCE_DOMAIN:-}
REMOTE_DIR=${SOURCE_ROOT:-/scratch/ebpfm}
SSH_KEY=${SSH_KEY:-$HOME/.ssh/id_ed25519}
RSH="ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o LogLevel=ERROR"

mkdir -p "$DEST" && chmod 700 "$DEST" || exit 2
failed=0
for host in "${HOSTS[@]}"; do
  echo "== $host"
  rsync -rlt --prune-empty-dirs --include='*/' --include='*.jsonl.gz' --exclude='*' \
        --timeout=600 --out-format='  %n (%l bytes)' -e "$RSH" \
        "$SOURCE_USER@$host${DOMAIN:+.$DOMAIN}:$REMOTE_DIR/" "$DEST/$host/"
  rc=$?
  if [ "$rc" -eq 0 ] || [ "$rc" -eq 24 ]; then   # 24: a file vanished mid-transfer
    echo "$host: ok"
  else
    echo "$host: FAILED (rsync exit $rc); run again to retry"
    failed=$((failed + 1))
  fi
done
echo "pulled into $DEST ($(du -sh "$DEST" 2>/dev/null | cut -f1)); $failed of ${#HOSTS[@]} nodes failed"
echo "next: python3 $(cd "$(dirname "$0")" && pwd)/seed_mirror.py $DEST <the receiver's base>"
exit $(( failed > 0 ))
