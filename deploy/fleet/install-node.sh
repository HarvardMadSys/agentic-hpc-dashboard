#!/bin/bash
# install-node.sh [--key 'PUBLIC KEY'] [--from ADDRESS] [--no-bootstrap] [--no-check]:
# one node of a fleet. Run as root on the node, from a checkout of this repository.
#
#   1. deploy/common/install-collector.sh: the collector, both tiers, as systemd units,
#      their output readable by one group.
#   2. The account the dashboard's host pulls as (PULL_USER, rc-pull), in that group: it
#      can read the collector's output, and no more than any account can otherwise.
#   3. With --key (the dashboard host's ETC/pull_key.pub), that key as the account's one
#      way in: read-only rsync of the collector's folders only (`rrsync -ro`, where this
#      host has rrsync), and with --from only from the dashboard's address.
#
# It prints the FLEET_EBPF_SOURCE and FLEET_NODE_SOURCE that fleet.env needs for this key.
# From the environment: PULL_USER, EBPF_DIR, NODE_DIR, COLLECTOR_GROUP (install-collector.sh).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
PULL_USER=${PULL_USER:-rc-pull}
DEFAULTS=${EBPFM_DEFAULT_FILE:-/etc/default/ebpfm}

say() { printf '==> %s\n' "$*"; }
die() { printf 'install-node.sh: %s\n' "$*" >&2; exit 1; }
# shellcheck source=SCRIPTDIR/../common/collector-paths.sh
. "$repo/deploy/common/collector-paths.sh"
usage() {
  echo "usage: install-node.sh [--key 'PUBLIC KEY'] [--from ADDRESS] [--no-bootstrap] [--no-check]" >&2
  exit 2
}

key='' from='' pass=()
while [ $# -gt 0 ]; do
  case $1 in
    --key) [ $# -ge 2 ] || usage; key=$2; shift 2 ;;
    --from) [ $# -ge 2 ] || usage; from=$2; shift 2 ;;
    --no-bootstrap|--no-check) pass+=("$1"); shift ;;
    *) usage ;;
  esac
done
[ "$(id -u)" = 0 ] || die "run as root"
[[ $PULL_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || die "PULL_USER='$PULL_USER' is not an account name"
if [ -n "$key" ]; then
  [[ $key =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-[a-z0-9@.-]+)\ [A-Za-z0-9+/=]+(\ [A-Za-z0-9@._-]+)?$ ]] \
    || die "--key: not one OpenSSH public key line (the contents of pull_key.pub)"
fi
[ -z "$from" ] || [[ $from =~ ^[A-Za-z0-9.:*?,/!-]+$ ]] || die "--from: not an address list"
command -v rsync >/dev/null 2>&1 || die "no rsync: the dashboard's host copies the collector's files with it (dnf/apt install rsync)"

"$repo/deploy/common/install-collector.sh" ${pass[@]+"${pass[@]}"}
group=$( (sed -n 's/^EBPFM_GROUP=//p' "$DEFAULTS" 2>/dev/null || true) | tail -n 1)
[ -n "$group" ] || die "$DEFAULTS names no EBPFM_GROUP after install-collector.sh"
# Where the collector writes, as install-collector.sh resolved it: what the key may read.
collector_paths

# The account: a shell, so sshd can run rsync's server (or rrsync) for it.
if ! id "$PULL_USER" >/dev/null 2>&1; then
  say "account $PULL_USER"
  useradd --system --create-home --home-dir "/var/lib/$PULL_USER" --shell /bin/sh \
    --comment "rc dashboard: copies the collector's files" "$PULL_USER"
fi
usermod -a -G "$group" "$PULL_USER"
say "$PULL_USER reads the collector's output as group $group"

# The folder both tiers share, as rrsync's root; the sources, as rrsync sees them.
root=$(dirname "$EBPF_DIR")
while [ "$root" != / ] && [ "$NODE_DIR" != "$root" ] && [ "${NODE_DIR#"$root"/}" = "$NODE_DIR" ]; do
  root=$(dirname "$root")
done
if [ "$root" = / ]; then
  rel_ebpf=${EBPF_DIR#/} rel_node=${NODE_DIR#/}
else
  rel_ebpf=${EBPF_DIR#"$root"/} rel_node=${NODE_DIR#"$root"/}
fi
[ "$NODE_DIR" = "$root" ] && rel_node=.

if [ -z "$key" ]; then
  say "no --key: give the dashboard host's pull_key.pub to $PULL_USER with --key, or by hand"
  say "(deploy/fleet/README.md, \"The nodes\"). fleet.env on the dashboard's host:"
  echo "    FLEET_EBPF_SOURCE=$EBPF_DIR"
  echo "    FLEET_NODE_SOURCE=$NODE_DIR"
  exit 0
fi

home=$(getent passwd "$PULL_USER" | cut -d: -f6)
if [ -z "$home" ] || [ ! -d "$home" ]; then
  die "$PULL_USER has no home folder for its authorized_keys"
fi
opts=restrict
[ -n "$from" ] && opts="$opts,from=\"$from\""
rr=${RRSYNC-$(command -v rrsync || true)}    # RRSYNC: another rrsync, or empty for none
if [ -n "$rr" ]; then
  opts="$opts,command=\"$rr -ro $root\""
  [ "$root" = / ] && say "warning: $EBPF_DIR and $NODE_DIR share no folder but /: rrsync lets the key read whatever $PULL_USER can"
  src_ebpf=$rel_ebpf src_node=$rel_node
else
  say "warning: no rrsync on this host: the key may run any command as $PULL_USER, who can read"
  say "the collector's output. An rsync package that installs rrsync restricts it (README.md, \"The nodes\")."
  src_ebpf=$EBPF_DIR src_node=$NODE_DIR
fi
line="$opts $key"
install -d -m 700 -o "$PULL_USER" -g "$(id -gn "$PULL_USER")" "$home/.ssh"
ak=$home/.ssh/authorized_keys
touch "$ak"
# This key's line replaces any line it had before; every other line stays.
blob=$(printf '%s\n' "$key" | cut -d' ' -f2)
{ grep -vF -- "$blob" "$ak" || true; printf '%s\n' "$line"; } > "$ak.new"
chown "$PULL_USER:$(id -gn "$PULL_USER")" "$ak.new"
chmod 600 "$ak.new"
mv -f "$ak.new" "$ak"
say "$ak: $line"
say "fleet.env on the dashboard's host, for this key:"
echo "    FLEET_EBPF_SOURCE=$src_ebpf"
echo "    FLEET_NODE_SOURCE=$src_node"
