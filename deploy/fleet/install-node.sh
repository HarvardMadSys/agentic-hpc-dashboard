#!/bin/bash
# install-node.sh [--key 'PUBLIC KEY'] [--from ADDRESS] [--name NAME] [--no-bootstrap]
#   [--no-check] [--restart] [--plan] [--help]: one node of a fleet. Run as root on the
# node, from a checkout of this repository.
#
#   1. deploy/common/install-collector.sh: the collector, both tiers, as systemd units,
#      their output readable by one group.
#   2. The account the dashboard's host pulls as (PULL_USER, rc-pull), in that group: it
#      can read the collector's output, and no more than any account can otherwise.
#   3. With --key (the dashboard host's ETC/pull_key.pub), that key as the account's one
#      way in: read-only rsync of the collector's folders only (`rrsync -ro`, where this
#      host has rrsync), and with --from only from the dashboard's address. Without --key,
#      a key installed before stays as it is.
#
# It prints this node's line for the dashboard host's fleet.env, ready to add there (or
# to give `deploy/rc-deploy add-node`): its name (--name, or this host's short name), how
# the dashboard's host reaches it (this host's full name: change it if that does not
# resolve there), and the folders this key reads, which differ with and without rrsync:
#   FLEET_HOSTS+=" node03=node03.example.org:ebpfm:ebpfm-login"
# --plan prints what it would do and changes nothing.
#
# From the environment: PULL_USER, EBPF_DIR, NODE_DIR, COLLECTOR_GROUP (install-collector.sh).
# PULL_USER is kept for the next run in /etc/rc-dashboard/install.env.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
DEFAULTS=${EBPFM_DEFAULT_FILE:-/etc/default/ebpfm}

say() { printf '==> %s\n' "$*"; }
die() { printf 'install-node.sh: %s\n' "$*" >&2; exit 1; }
# shellcheck source=SCRIPTDIR/../common/lib.sh
. "$repo/deploy/common/lib.sh"
# shellcheck source=SCRIPTDIR/../common/collector-paths.sh
. "$repo/deploy/common/collector-paths.sh"
usage() {
  echo "usage: install-node.sh [--key 'PUBLIC KEY'] [--from ADDRESS] [--name NAME] [--no-bootstrap] [--no-check] [--restart] [--plan] [--help]" >&2
  exit 2
}

key='' from='' name='' pass=()
while [ $# -gt 0 ]; do
  case $1 in
    --key) [ $# -ge 2 ] || usage; key=$2; shift 2 ;;
    --from) [ $# -ge 2 ] || usage; from=$2; shift 2 ;;
    --name) [ $# -ge 2 ] || usage; name=$2; shift 2 ;;
    --no-bootstrap|--no-check|--restart) pass+=("$1"); shift ;;
    --plan) PLAN=1; pass+=("$1"); shift ;;
    -h|--help) show_help "$0"; exit 0 ;;
    *) usage ;;
  esac
done
if [ "$(id -u)" != 0 ]; then
  [ "$PLAN" = 1 ] || die "run as root"
fi
install_env_load PULL_USER
PULL_USER=${PULL_USER:-rc-pull}
[[ $PULL_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || die "PULL_USER='$PULL_USER' is not an account name"
# A key pasted from a terminal may carry a carriage return or blanks around it.
key=$(printf '%s' "$key" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
if [ -n "$key" ]; then
  [[ $key =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-[a-z0-9@.-]+)\ [A-Za-z0-9+/=]+(\ [A-Za-z0-9@._-]+)?$ ]] \
    || die "--key: not one OpenSSH public key line (the contents of pull_key.pub)"
fi
[ -z "$from" ] || [[ $from =~ ^[A-Za-z0-9.:*?,/!-]+$ ]] || die "--from: not an address list"
name=${name:-$(hostname -s)}
[[ $name =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "--name: '$name' is not a node name (letters, digits, . _ -)"
command -v rsync >/dev/null 2>&1 || die "no rsync: the dashboard's host copies the collector's files with it (dnf/apt install rsync)"

"$repo/deploy/common/install-collector.sh" ${pass[@]+"${pass[@]}"}
group=$(_ebpfm_default EBPFM_GROUP)      # without the quotes systemd takes off
if [ -z "$group" ]; then
  [ "$PLAN" = 1 ] || die "$DEFAULTS names no EBPFM_GROUP after install-collector.sh"
  group=${COLLECTOR_GROUP:-ebpfm}
fi
# Where the collector writes, as install-collector.sh resolved it: what the key may read.
FROM_EBPF_DIR=env FROM_NODE_DIR=env
collector_paths

# The account: a shell, so sshd can run rsync's server (or rrsync) for it.
if ! id "$PULL_USER" >/dev/null 2>&1; then
  say "account $PULL_USER"
  run useradd --system --create-home --home-dir "/var/lib/$PULL_USER" --shell /bin/sh \
    --comment "rc dashboard: copies the collector's files" "$PULL_USER"
fi
run usermod -a -G "$group" "$PULL_USER"
say "$PULL_USER reads the collector's output as group $group"
install_env_save PULL_USER

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
fqdn=$(hostname -f 2>/dev/null || hostname)
entry() {       # EBPF_SOURCE NODE_SOURCE: this node's line in fleet.env
  say "this node's line for fleet.env on the dashboard's host (or: deploy/rc-deploy add-node '$name=$fqdn:$1:$2'):"
  echo "    FLEET_HOSTS+=\" $name=$fqdn:$1:$2\""
  echo "    (after = is how the dashboard's host reaches this node: change it if that name does not resolve there)"
}

home=$(getent passwd "$PULL_USER" | cut -d: -f6 || true)
ak=${home:-/var/lib/$PULL_USER}/.ssh/authorized_keys
rr=${RRSYNC-$(command -v rrsync || true)}    # RRSYNC: another rrsync, or empty for none
if [ -z "$key" ]; then
  if [ -s "$ak" ]; then
    say "no --key: the key already in $ak stays as it is"
    if grep -q 'command="[^"]*rrsync -ro ' "$ak"; then
      entry "$rel_ebpf" "$rel_node"
    else
      entry "$EBPF_DIR" "$NODE_DIR"
    fi
  else
    say "no --key: give the dashboard host's pull_key.pub to $PULL_USER with --key, or by hand"
    say "(deploy/fleet/README.md, \"The nodes\"). With rrsync it reads the folders as below;"
    say "without it, by their full paths ($EBPF_DIR, $NODE_DIR)."
    if [ -n "$rr" ]; then entry "$rel_ebpf" "$rel_node"; else entry "$EBPF_DIR" "$NODE_DIR"; fi
  fi
  [ "$PLAN" = 1 ] && say "the plan ends here: nothing was changed"
  exit 0
fi

if [ -z "$home" ] || { [ ! -d "$home" ] && [ "$PLAN" != 1 ]; }; then
  die "$PULL_USER has no home folder for its authorized_keys"
fi
opts=restrict
[ -n "$from" ] && opts="$opts,from=\"$from\""
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
run install -d -m 700 -o "$PULL_USER" -g "$(id -gn "$PULL_USER" 2>/dev/null || echo "$PULL_USER")" "$home/.ssh"
# This key's line replaces any line it had before; every other line stays.
blob=$(printf '%s\n' "$key" | cut -d' ' -f2)
if [ "$PLAN" = 1 ]; then
  { grep -vF -- "$blob" "$ak" 2>/dev/null || true; printf '%s\n' "$line"; } | put "$ak"
else
  touch "$ak"
  { grep -vF -- "$blob" "$ak" || true; printf '%s\n' "$line"; } > "$ak.new"
  chown "$PULL_USER:$(id -gn "$PULL_USER")" "$ak.new"
  chmod 600 "$ak.new"
  mv -f "$ak.new" "$ak"
  say "$ak: $line"
fi
entry "$src_ebpf" "$src_node"
[ "$PLAN" = 1 ] && say "the plan ends here: nothing was changed"
exit 0
