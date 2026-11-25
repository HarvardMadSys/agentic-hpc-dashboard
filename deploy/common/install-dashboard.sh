#!/bin/bash
# install-dashboard.sh single|fleet [--no-build] [--plan] [--help]: run the dashboard on
# this host under systemd. Run as root, from a checkout of this repository; the service
# runs the code in it.
#
#   single  The dashboard reads this host's collector (EBPF_DIR, NODE_DIR), as a member of
#           the group the collector writes for (EBPFM_GROUP in /etc/default/ebpfm).
#   fleet   The dashboard reads the copy that deploy/fleet/pull.py keeps of every node's
#           files, and rc-pull.service runs pull.py, as the same account.
#
# It makes the account SERVICE_USER; the venv in OPT (uv, which brings a Python of its own
# where this host has none new enough); the page's bundle (npm), unless --no-build; and in
# ETC:
#   dashboard.env  your settings: the port, the site's name, Slurm or not. Written only
#                  when there is none, so your edits stay; a setting given below is set
#                  in it, and the change printed.
#   paths.env      where the dashboard reads and keeps things, and the password's hash
#                  file once there is one. Written on every run.
#   password.hash  the password's hash, which deploy/common/password.sh writes; the
#                  dashboard reads it again when it changes.
#   proxy.secret   with single sign-on (RC_DASH_PROXY_HEADER in dashboard.env), the
#                  secret the proxy sends on every request, made if missing.
#   fleet.env      (fleet) the nodes pull.py copies, written only when there is none,
#   pull_key       (fleet) and the ssh key it logs in to them with, made if missing.
# Then the units, enabled: rc-dashboard.service, its memory cap in the drop-in
# rc-dashboard.service.d/50-memory.conf (written when there is none or MEMORY_MAX is
# given, so `systemctl edit rc-dashboard` stays), rc-dashboard-check.timer (the hourly
# check, deploy/common/check.sh), and rc-pull.service (fleet). rc-pull starts once
# fleet.env names your nodes; the dashboard starts once it has a password (or single
# sign-on through a proxy, RC_DASH_PROXY_HEADER, or RC_DASH_OPEN=1), and is restarted by a
# run that finds it running, to pick up the new code.
#
# --plan prints what it would do and changes nothing (--help: this text).
#
# The installer's own answers come from the environment, else from
# /etc/rc-dashboard/install.env, where every run saves them (and prints any that
# changed), else the defaults: SERVICE_USER (rc-dashboard), PULL_USER (rc-pull: the account
# on the nodes, whose processes the dashboard drops), ETC (/etc/rc-dashboard), STATE
# (/var/lib/rc-dashboard), OPT (/opt/rc-dashboard), MEMORY_MAX (8G; DASH_MEMORY_MAX is the
# same), and for single EBPF_DIR, NODE_DIR, COLLECTOR_GROUP (the folders and group
# /etc/default/ebpfm names win: collector-paths.sh).
#
# Settings that go into dashboard.env (and fleet.env), under the names every kit uses:
# SITE_NAME, SITE_DESCRIPTION, SLURM (yes/no: the HPC tab), NODE_NOUN ("servers" without
# Slurm, "login nodes" with it), ANONYMIZE (yes/no); for fleet NODES (FLEET_HOSTS of the
# fleet.env the first install makes) and FREE_MIN_GB. Yes/no takes 1/0, true/false, yes/no
# or on/off.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
DEFAULTS=${EBPFM_DEFAULT_FILE:-/etc/default/ebpfm}

say() { printf '==> %s\n' "$*"; }
die() { printf 'install-dashboard.sh: %s\n' "$*" >&2; exit 1; }
usage() { echo "usage: install-dashboard.sh single|fleet [--no-build] [--plan] [--help]" >&2; exit 2; }
# shellcheck source=SCRIPTDIR/lib.sh
. "$here/lib.sh"
# shellcheck source=SCRIPTDIR/collector-paths.sh
. "$here/collector-paths.sh"

mode=${1:-}
case $mode in
  single|fleet) shift ;;
  -h|--help) show_help "$0"; exit 0 ;;
  *) usage ;;
esac
build=1
for a in "$@"; do
  case $a in
    --no-build) build=0 ;;
    --plan) PLAN=1 ;;
    -h|--help) show_help "$0"; exit 0 ;;
    *) usage ;;
  esac
done
[ "$PLAN" = 1 ] && say "the plan: nothing below is done"

if [ "$(id -u)" != 0 ]; then
  [ "$PLAN" = 1 ] || die "run as root: it makes an account and installs units"
  say "not root: files only root may read are taken as missing, so the plan may differ"
fi
command -v systemctl >/dev/null 2>&1 || die "no systemctl: the dashboard runs as a systemd unit"

# ---- the answers: the environment, else install.env, else what an install from before
#      install.env left in its unit, else the defaults
if [ -z "${MEMORY_MAX:-}" ] && [ -n "${DASH_MEMORY_MAX:-}" ]; then
  MEMORY_MAX=$DASH_MEMORY_MAX
fi
install_env_load SERVICE_USER PULL_USER ETC STATE OPT MEMORY_MAX
FROM_EBPF_DIR=env FROM_NODE_DIR=env
unit_file=$UNIT_DIR/rc-dashboard.service
recover() {     # VAR VALUE: an answer only the installed unit remembers
  local from=FROM_$1
  [ "${!from}" = default ] && [ -n "$2" ] || return 0
  printf -v "$1" '%s' "$2"
  printf -v "$from" '%s' unit
  say "$1=$2, as the installed $(basename "$unit_file") has it"
}
# (install-collector.sh saves its own keys there first: an install.env without MODE was
# never written by this installer.)
if [ -z "$(env_value "$INSTALL_ENV" MODE)" ] && [ -f "$unit_file" ]; then
  recover SERVICE_USER "$(sed -n 's/^User=//p' "$unit_file" | tail -n 1)"
  recover ETC "$(sed -n 's#^EnvironmentFile=\(.*\)/dashboard\.env$#\1#p' "$unit_file" | tail -n 1)"
  recover OPT "$(sed -n 's#^ExecStart=\(.*\)/venv/bin/python .*#\1#p' "$unit_file" | tail -n 1)"
  recover MEMORY_MAX "$(sed -n 's/^MemoryMax=//p' "$unit_file" | tail -n 1)"
  recover STATE "$(env_value "${ETC:-/etc/rc-dashboard}/paths.env" RC_DASH_CACHE_DIR | sed 's#/cache$##')"
  recover PULL_USER "$(env_value "${ETC:-/etc/rc-dashboard}/fleet.env" FLEET_USER)"
fi
SERVICE_USER=${SERVICE_USER:-rc-dashboard}
PULL_USER=${PULL_USER:-rc-pull}
ETC=${ETC:-/etc/rc-dashboard}
STATE=${STATE:-/var/lib/rc-dashboard}
OPT=${OPT:-/opt/rc-dashboard}
VENV=$OPT/venv
MODE=$mode
# The memory cap lives in its drop-in, which `systemctl edit rc-dashboard` changes: the
# drop-in's value stands unless MEMORY_MAX is given now.
dropin=$UNIT_DIR/rc-dashboard.service.d/50-memory.conf
inplace=$( (sed -n 's/^MemoryMax=//p' "$dropin" 2>/dev/null || true) | tail -n 1)
if [ -n "$inplace" ] && [ "$FROM_MEMORY_MAX" != env ]; then
  MEMORY_MAX=$inplace
fi
MEMORY_MAX=${MEMORY_MAX:-8G}

for v in SERVICE_USER PULL_USER; do
  [[ ${!v} =~ ^[a-z_][a-z0-9_-]*$ ]] || die "$v='${!v}' is not an account name"
done
for v in ETC STATE OPT; do
  [[ ${!v} =~ ^/[A-Za-z0-9._/@+-]*$ ]] || die "$v='${!v}' must be an absolute path, with no spaces or quotes"
done
[[ $repo =~ ^/[A-Za-z0-9._/@+-]*$ ]] || die "the checkout's path ($repo) must have no spaces or quotes: the units name it"
[[ $MEMORY_MAX =~ ^[1-9][0-9]*[KMGT]$ ]] || die "MEMORY_MAX='$MEMORY_MAX' must be a size such as 4G"
# The settings that go into dashboard.env, checked before anything changes.
for v in SLURM ANONYMIZE; do
  if [ -n "${!v:-}" ]; then
    word=$(yesno "$v" "${!v}")
    printf -v "$v" '%s' "$word"
  fi
done
[ -z "${FREE_MIN_GB:-}" ] || [[ $FREE_MIN_GB =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "FREE_MIN_GB='$FREE_MIN_GB' is not a number"

group=
if [ "$mode" = single ]; then
  named=$(_ebpfm_default EBPFM_GROUP)    # without the quotes systemd takes off
  group=${COLLECTOR_GROUP:-$named}
  if [ -z "$group" ]; then
    [ "$PLAN" = 1 ] || die "no group can read the collector's output: run install-collector.sh first (it sets EBPFM_GROUP in $DEFAULTS), or set COLLECTOR_GROUP"
    group=ebpfm
    say "(the collector's group, $group, is made by install-collector.sh)"
  fi
  if ! getent group "$group" >/dev/null; then
    [ "$PLAN" = 1 ] || die "group $group does not exist: run install-collector.sh first"
  fi
  # Where the collector writes, which is where the dashboard reads.
  collector_paths
  for v in EBPF_DIR NODE_DIR; do
    [[ ${!v} =~ ^/[A-Za-z0-9._/@+-]*$ ]] || die "$v='${!v}' must be an absolute path, with no spaces or quotes"
  done
fi

# ---- the account, and its folders
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  say "account $SERVICE_USER"
  run useradd --system --user-group --home-dir "$STATE" --no-create-home \
    --shell /usr/sbin/nologin --comment "rc dashboard" "$SERVICE_USER"
fi
if [ "$mode" = single ] && ! id -nG "$SERVICE_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$group"; then
  # A member of the collector's group in /etc/group, not only through the unit's
  # SupplementaryGroups=: `sudo -u rc-dashboard ... check-feeds` then reads what it reads.
  say "$SERVICE_USER joins group $group"
  run usermod -a -G "$group" "$SERVICE_USER"
fi
run install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE"
run install -d -m 750 -o root -g "$SERVICE_USER" "$ETC"
run install -d -m 755 "$OPT"
# The service reads the code from this checkout, and runs as an account that is not yours.
if id "$SERVICE_USER" >/dev/null 2>&1; then
  if ! runuser -u "$SERVICE_USER" -- test -r "$repo/dashboard/rc_dashboard/__init__.py"; then
    die "$SERVICE_USER cannot read $repo: clone the repository where any account can read it
  (such as /opt/agentic-hpc-dashboard), or open the folders on its path to it"
  fi
fi

# ---- the code's runtime: the venv and the page's bundle, readable by the service
umask 022
if [ "$build" = 1 ]; then
  command -v uv >/dev/null 2>&1 || die "no uv: install it (https://docs.astral.sh/uv/), or --no-build with a venv already in $VENV"
  say "venv in $VENV"
  run env -C "$repo/dashboard" UV_PROJECT_ENVIRONMENT="$VENV" UV_PYTHON_INSTALL_DIR="$OPT/python" \
    uv sync --frozen --no-dev --no-install-project --quiet
  command -v npm >/dev/null 2>&1 || die "no npm: install Node.js 20 or later, or --no-build with the bundle already in dashboard/web/dist"
  say "the page's bundle in dashboard/web/dist"
  run env -C "$repo/dashboard/web" npm ci --no-audit --no-fund --loglevel=error
  run env -C "$repo/dashboard/web" npm run build --silent
fi
if [ "$PLAN" != 1 ]; then
  [ -x "$VENV/bin/python" ] || die "no venv at $VENV: run this without --no-build"
  [ -f "$repo/dashboard/web/dist/index.html" ] || die "no bundle in $repo/dashboard/web/dist: run this without --no-build"
fi

# ---- settings
umask 027
render() {     # TEMPLATE: the template with this install's names filled in
  sed -e "s#@REPO@#$repo#g" -e "s#@VENV@#$VENV#g" -e "s#@ETC@#$ETC#g" \
      -e "s#@STATE@#$STATE#g" -e "s#@USER@#$SERVICE_USER#g" -e "s#@PULL_USER@#$PULL_USER#g" \
      -e "s#@GROUP@#$group#g" -e "s#@MEMORY_MAX@#$MEMORY_MAX#g" "$1"
}
# A settings file: made from its template when there is none, then each setting given
# set in it. With --plan the new file is shown as it would be.
settings_file() {     # FILE TEMPLATE KEY=VALUE...
  local f=$1 tmpl=$2 kv work plan=$PLAN
  shift 2
  if [ -e "$f" ] || [ "$plan" = 1 ]; then
    work=$(mktemp)
    if [ -e "$f" ]; then cat "$f" > "$work"; else render "$tmpl" > "$work"; fi
  else
    say "$f: made from $(basename "$tmpl")"
    render "$tmpl" > "$f"
    work=$f
  fi
  PLAN=0 SHOWN_AS=$f
  for kv in "$@"; do set_env_line "$work" "${kv%%=*}" "${kv#*=}"; done
  PLAN=$plan SHOWN_AS=
  if [ "$work" != "$f" ]; then
    put "$f" < "$work"
    rm -f "$work"
  fi
}

given=()
if [ ! -e "$ETC/dashboard.env" ] && [ -n "${SLURM:-}" ]; then
  # A new deployment, told whether it runs Slurm: the hosts' noun and the name follow.
  if [ "$SLURM" = 1 ]; then NODE_NOUN=${NODE_NOUN:-login nodes}; else NODE_NOUN=${NODE_NOUN:-servers}; fi
  SITE_NAME=${SITE_NAME:-$NODE_NOUN · live}
fi
[ -n "${SITE_NAME:-}" ] && given+=("RC_DASH_SITE_NAME=$SITE_NAME")
[ -n "${SITE_DESCRIPTION:-}" ] && given+=("RC_DASH_SITE_DESC=$SITE_DESCRIPTION")
[ -n "${SLURM:-}" ] && given+=("RC_DASH_SLURM=$SLURM")
[ -n "${NODE_NOUN:-}" ] && given+=("RC_DASH_NODE_NOUN=$NODE_NOUN")
[ -n "${ANONYMIZE:-}" ] && given+=("RC_DASH_ANONYMIZE=$ANONYMIZE")
if [ ! -e "$ETC/dashboard.env" ] && [ -z "${SLURM:-}" ]; then
  say "SLURM not given: the page starts with no HPC tab and calls the hosts \"servers\""
  say "(RC_DASH_SLURM=0). On login nodes that run Slurm, run this again with SLURM=1,"
  say "or use deploy/rc-deploy init, which asks."
fi
settings_file "$ETC/dashboard.env" "$here/dashboard.env.example" ${given[@]+"${given[@]}"}
run chown root:"$SERVICE_USER" "$ETC/dashboard.env"
run chmod 640 "$ETC/dashboard.env"

pullpy() {      # pull.py, on the venv's Python where there is one (it needs 3.6 alone)
  local py=$VENV/bin/python
  [ -x "$py" ] || py=python3
  "$py" "$repo/deploy/fleet/pull.py" --config "$ETC/fleet.env" "$@"
}
pulling=0
roots=
if [ "$mode" = fleet ]; then
  fleet=()
  [ -n "${FREE_MIN_GB:-}" ] && fleet+=("FLEET_FREE_MIN_GB=$FREE_MIN_GB")
  # NODES names the nodes of the fleet.env made now, and of no later one: rc-deploy
  # hands deploy/site.env's NODES to every apply and upgrade, and a fleet emptied on
  # purpose (remove-node of its last node) would get the retired nodes back.
  if [ -n "${NODES:-}" ]; then
    if [ ! -e "$ETC/fleet.env" ]; then
      fleet+=("FLEET_HOSTS=$NODES")
    else
      say "NODES given, but $ETC/fleet.env is made already, and its FLEET_HOSTS names the"
      say "nodes: NODES is left out. Add or remove one with deploy/rc-deploy add-node /"
      say "remove-node, or in the file"
    fi
  fi
  settings_file "$ETC/fleet.env" "$repo/deploy/fleet/fleet.env.example" ${fleet[@]+"${fleet[@]}"}
  run chown root:"$SERVICE_USER" "$ETC/fleet.env"
  run chmod 640 "$ETC/fleet.env"
  if [ ! -e "$ETC/pull_key" ]; then
    say "$ETC/pull_key: the key pull.py logs in to the nodes with"
    run ssh-keygen -q -t ed25519 -N '' -C "rc-pull@$(hostname -s)" -f "$ETC/pull_key"
  fi
  run chown "$SERVICE_USER:$SERVICE_USER" "$ETC/pull_key" "$ETC/pull_key.pub"
  run chmod 600 "$ETC/pull_key"
  run chmod 644 "$ETC/pull_key.pub"
  # Configured once pull.py finds nodes in fleet.env (exit 4: none yet). The old
  # template's example nodes are none.
  if [ ! -e "$ETC/fleet.env" ]; then
    rc=4
  elif grep -qx 'FLEET_HOSTS="server01 server02"' "$ETC/fleet.env"; then
    say "fleet.env still names the example's nodes, server01 and server02: name yours"
    rc=4
  else
    set +e
    out=$(pullpy check 2>&1)
    rc=$?
    set -e
  fi
  case $rc in
    0) roots=$(pullpy roots) || die "$ETC/fleet.env: see the message above"
       pulling=1 ;;
    4) roots="RC_DASH_EBPF_ROOTS=$STATE/mirror/ebpf/*
RC_DASH_NODE_ROOTS=$STATE/mirror/node/*
RC_DASH_TRANSPORT_FILES=$STATE/mirror/.state/*.json" ;;
    *) die "$ETC/fleet.env: $out" ;;
  esac
else
  roots="RC_DASH_EBPF_ROOTS=$EBPF_DIR
RC_DASH_NODE_ROOTS=$NODE_DIR"
fi
# Single sign-on through a proxy (RC_DASH_PROXY_HEADER in dashboard.env): the secret the
# proxy sends on every request, made once, readable by root and the dashboard alone. The
# dashboard believes the user the proxy names only with it: every account on this host
# connects from 127.0.0.1 too.
proxy_on=
if [ -n "$(env_value "$ETC/dashboard.env" RC_DASH_PROXY_HEADER 2>/dev/null || true)" ]; then
  proxy_on=1
  if [ ! -s "$ETC/proxy.secret" ]; then
    say "$ETC/proxy.secret: the secret the proxy sends the dashboard on every request"
    if [ "$PLAN" = 1 ]; then
      echo "    (made from /dev/urandom, mode 640 root:$SERVICE_USER)"
    else
      (umask 077 && head -c 48 /dev/urandom | base64 | tr -d '\n/+=' > "$ETC/proxy.secret")
    fi
  fi
  run chown root:"$SERVICE_USER" "$ETC/proxy.secret"
  run chmod 640 "$ETC/proxy.secret"
fi
{
  echo "# Written by deploy/common/install-dashboard.sh ($mode) on every run; your settings"
  echo "# are in dashboard.env. Where the dashboard reads, and where it keeps things:"
  echo "$roots"
  echo "RC_DASH_STATIC_DIR=$repo/dashboard/web/dist"
  echo "RC_DASH_CACHE_DIR=$STATE/cache"
  if [ -s "$ETC/password.hash" ]; then
    echo "# The password's hash (deploy/common/password.sh), read again when it changes."
    echo "RC_DASH_PASSWORD_HASH_FILE=$ETC/password.hash"
  fi
  if [ -n "$proxy_on" ]; then
    echo "# The secret the single sign-on's proxy sends in X-RC-Proxy-Secret."
    echo "RC_DASH_PROXY_SECRET_FILE=$ETC/proxy.secret"
  fi
} | put "$ETC/paths.env"
run chown root:"$SERVICE_USER" "$ETC/paths.env"
run chmod 640 "$ETC/paths.env"

# ---- units
umask 022
say "units in $UNIT_DIR"
render "$here/rc-dashboard.service.in" | put "$unit_file"
if [ ! -e "$dropin" ] || [ "$inplace" != "$MEMORY_MAX" ]; then
  [ -n "$inplace" ] && [ "$inplace" != "$MEMORY_MAX" ] && say "MEMORY_MAX: $inplace -> $MEMORY_MAX ($dropin)"
  run mkdir -p "$(dirname "$dropin")"
  render "$here/rc-dashboard-memory.conf.in" | put "$dropin"
fi
render "$here/rc-dashboard-check.service.in" | put "$UNIT_DIR/rc-dashboard-check.service"
render "$here/rc-dashboard-check.timer.in" | put "$UNIT_DIR/rc-dashboard-check.timer"
if [ "$mode" = fleet ]; then
  render "$repo/deploy/fleet/rc-pull.service.in" | put "$UNIT_DIR/rc-pull.service"
fi
run systemctl daemon-reload
run systemctl enable --quiet rc-dashboard.service
run systemctl enable --quiet --now rc-dashboard-check.timer

if [ "$mode" = fleet ]; then
  run systemctl enable --quiet rc-pull.service
  if [ "$pulling" = 1 ]; then
    if systemctl is-active --quiet rc-pull.service; then
      run systemctl restart rc-pull.service
    else
      run systemctl start rc-pull.service
    fi
    say "rc-pull runs: $VENV/bin/python $repo/deploy/fleet/pull.py --config $ETC/fleet.env status"
  else
    say "rc-pull waits for your nodes: give each this key (deploy/fleet/README.md, \"The nodes\"),"
    say "add the line install-node.sh prints there to $ETC/fleet.env (or deploy/rc-deploy add-node),"
    say "and run this again:"
    if [ -e "$ETC/pull_key.pub" ]; then cat "$ETC/pull_key.pub"; fi
  fi
fi

install_env_save MODE SERVICE_USER PULL_USER ETC STATE OPT MEMORY_MAX

set_in() { [ -n "$(env_value "$ETC/dashboard.env" "$1" 2>/dev/null)" ]; }
open_in() { [ "$(yesno RC_DASH_OPEN "$(env_value "$ETC/dashboard.env" RC_DASH_OPEN 2>/dev/null || true)" 2>/dev/null)" = 1 ]; }
if set_in RC_DASH_PASSWORD_HASH || [ -s "$ETC/password.hash" ] || set_in RC_DASH_PROXY_HEADER \
    || { set_in RC_DASH_OPEN && open_in; }; then
  if systemctl is-active --quiet rc-dashboard.service; then
    say "restarting the dashboard: it refills its history from the feeds"
    run systemctl restart rc-dashboard.service
  else
    run systemctl start rc-dashboard.service
  fi
  say "the dashboard runs: systemctl status rc-dashboard"
  if [ -n "$proxy_on" ]; then
    say "single sign-on: the proxy must send X-RC-Proxy-Secret, with the secret in"
    echo "    $ETC/proxy.secret, on every request it passes on (dashboard/README.md,"
    echo "    \"Single sign-on through a proxy\"); without it, no one is signed in"
  fi
else
  say "the dashboard starts once it has a password. Make one, as root, with"
  echo "      $repo/deploy/common/password.sh        (or: $repo/deploy/rc-deploy password)"
  echo "    which keeps the password in your ~/.config/rc-dashboard, puts its hash in"
  echo "    $ETC/password.hash, and starts the dashboard. Or let the site's single sign-on"
  echo "    admit readers through a proxy: RC_DASH_PROXY_HEADER in $ETC/dashboard.env."
fi
[ "$PLAN" = 1 ] && say "the plan ends here: nothing was changed"
exit 0
