#!/bin/bash
# install-sender.sh SHA DIR: run on a source node by ship-sender.sh (as `bash -s` over
# ssh), after it copied the release's hpc/fleet/rsync_guard.py to DIR/.incoming-SHA. DIR
# (site.env's SENDER_DIR) is the one thing the deployment keeps on the nodes: the copy
# key's forced command runs DIR/current/rsync_guard.py there. Installs the file as
# DIR/SHA/rsync_guard.py, and as DIR/SHA/fleet_stream.py too, the name a key from the
# earlier stream runs it by (`fleet_stream.py send --root ROOT`, which the guard takes), so
# such a key keeps working; moves DIR/current to it, keeps the release it replaced for a
# rollback, and removes older ones (a guard that runs has its code loaded already).
# When DIR/current is SHA already (a home the nodes share, or a deploy of the same
# commit), nothing else changes, so the release kept for a rollback stays.
set -euo pipefail
umask 077
if [ $# -ne 2 ] || [ -z "$1" ] || [ -z "$2" ]; then
  echo "usage: install-sender.sh SHA DIR" >&2
  exit 2
fi
sha=$1 dir=$2
inc=$dir/.incoming-$sha
mkdir -p "$dir"
chmod 700 "$dir"
if [ -d "$dir/$sha" ]; then
  rm -f "$inc"                  # installed before; a release's file never changes
else
  if [ ! -s "$inc" ]; then
    echo "install-sender.sh: $inc is missing or empty; nothing installed" >&2
    rm -f "$inc"
    exit 1
  fi
  rm -rf "$dir/$sha.tmp"
  mkdir "$dir/$sha.tmp"
  mv "$inc" "$dir/$sha.tmp/rsync_guard.py"
  chmod 600 "$dir/$sha.tmp/rsync_guard.py"
  ln -s rsync_guard.py "$dir/$sha.tmp/fleet_stream.py"
  mv -T "$dir/$sha.tmp" "$dir/$sha"
fi
prev=$(readlink "$dir/current" 2>/dev/null || true)
if [ "$prev" = "$sha" ]; then
  echo "guard $sha already current in $dir"
  exit 0
fi
ln -sfn "$sha" "$dir/current.new"
mv -T "$dir/current.new" "$dir/current"
for d in "$dir"/*; do
  [ -d "$d" ] && [ ! -L "$d" ] || continue
  n=${d##*/}
  case $n in
    "$sha"|"$prev") ;;
    *.tmp) rm -rf "$d" ;;
    *) [[ $n =~ ^[0-9a-f]{12}$ ]] && rm -rf "$d" ;;
  esac
done
echo "guard $sha installed in $dir"
