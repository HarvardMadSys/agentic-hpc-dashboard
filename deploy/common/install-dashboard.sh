#!/bin/bash
# install-dashboard.sh single|fleet [--no-build]: run the dashboard on this host under
# systemd. Run as root, from a checkout of this repository; the service runs the code in it.
#
#   single  The dashboard reads this host's collector (EBPF_DIR, NODE_DIR), in the group the
#           collector writes for (EBPFM_GROUP in /etc/default/ebpfm).
#   fleet   The dashboard reads the copy that deploy/fleet/pull.py keeps of every node's
#           files, and rc-pull.service runs pull.py, as the same account.
#
# It makes the account SERVICE_USER; the venv in OPT (uv, which brings a Python of its own
# where this host has none new enough); the page's bundle (npm), unless --no-build; and in
# ETC:
#   dashboard.env  your settings: the password hash, the port, the site's name. Written
#                  only when there is none, so your edits stay.
#   paths.env      where the dashboard reads and keeps things. Written on every run.
#   fleet.env      (fleet) the nodes pull.py copies, written only when there is none,
#   pull_key       (fleet) and the ssh key it logs in to them with, made if missing.
# Then the units, enabled. rc-pull starts once fleet.env names your nodes; the dashboard
# starts once dashboard.env has a password hash (or RC_DASH_OPEN=1), and is restarted by
# a run that finds it running, to pick up the new code.
#
# From the environment: SERVICE_USER (rc-dashboard), PULL_USER (rc-pull: the account on the
# nodes, whose processes the dashboard drops), ETC (/etc/rc-dashboard), STATE
# (/var/lib/rc-dashboard), OPT (/opt/rc-dashboard), MEMORY_MAX (8G), and for single
# EBPF_DIR (/var/log/ebpfm), NODE_DIR (/var/log/ebpfm-login), COLLECTOR_GROUP. A folder
# /etc/default/ebpfm names is where the collector writes, and wins (collector-paths.sh).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
SERVICE_USER=${SERVICE_USER:-rc-dashboard}
PULL_USER=${PULL_USER:-rc-pull}
ETC=${ETC:-/etc/rc-dashboard}
STATE=${STATE:-/var/lib/rc-dashboard}
OPT=${OPT:-/opt/rc-dashboard}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
MEMORY_MAX=${MEMORY_MAX:-8G}
DEFAULTS=${EBPFM_DEFAULT_FILE:-/etc/default/ebpfm}
VENV=$OPT/venv

say() { printf '==> %s\n' "$*"; }
die() { printf 'install-dashboard.sh: %s\n' "$*" >&2; exit 1; }
usage() { echo "usage: install-dashboard.sh single|fleet [--no-build]" >&2; exit 2; }
# shellcheck source=SCRIPTDIR/collector-paths.sh
. "$here/collector-paths.sh"

mode=${1:-}
case $mode in single|fleet) shift ;; *) usage ;; esac
build=1
for a in "$@"; do
  case $a in --no-build) build=0 ;; *) usage ;; esac
done

[ "$(id -u)" = 0 ] || die "run as root: it makes an account and installs units"
command -v systemctl >/dev/null 2>&1 || die "no systemctl: the dashboard runs as a systemd unit"
for v in SERVICE_USER PULL_USER; do
  [[ ${!v} =~ ^[a-z_][a-z0-9_-]*$ ]] || die "$v='${!v}' is not an account name"
done
for v in ETC STATE OPT; do
  [[ ${!v} =~ ^/[A-Za-z0-9._/@+-]*$ ]] || die "$v='${!v}' must be an absolute path, with no spaces or quotes"
done
[[ $repo =~ ^/[A-Za-z0-9._/@+-]*$ ]] || die "the checkout's path ($repo) must have no spaces or quotes: the units name it"
[[ $MEMORY_MAX =~ ^[1-9][0-9]*[KMGT]$ ]] || die "MEMORY_MAX='$MEMORY_MAX' must be a size such as 4G"

group=
if [ "$mode" = single ]; then
  named=$( (sed -n 's/^EBPFM_GROUP=//p' "$DEFAULTS" 2>/dev/null || true) | tail -n 1)
  group=${COLLECTOR_GROUP:-$named}
  [ -n "$group" ] || die "no group can read the collector's output: run install-collector.sh first (it sets EBPFM_GROUP in $DEFAULTS), or set COLLECTOR_GROUP"
  getent group "$group" >/dev/null || die "group $group does not exist: run install-collector.sh first"
  # Where the collector writes, which is where the dashboard reads.
  collector_paths
  for v in EBPF_DIR NODE_DIR; do
    [[ ${!v} =~ ^/[A-Za-z0-9._/@+-]*$ ]] || die "$v='${!v}' must be an absolute path, with no spaces or quotes"
  done
fi

# ---- the account, and its folders
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  say "account $SERVICE_USER"
  useradd --system --user-group --home-dir "$STATE" --no-create-home \
    --shell /usr/sbin/nologin --comment "rc dashboard" "$SERVICE_USER"
fi
install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE"
install -d -m 750 -o root -g "$SERVICE_USER" "$ETC"
install -d -m 755 "$OPT"
# The service reads the code from this checkout, and runs as an account that is not yours.
if ! runuser -u "$SERVICE_USER" -- test -r "$repo/dashboard/rc_dashboard/__init__.py"; then
  die "$SERVICE_USER cannot read $repo: clone the repository where any account can read it
  (such as /opt/agentic-hpc-dashboard), or open the folders on its path to it"
fi

# ---- the code's runtime: the venv and the page's bundle, readable by the service
umask 022
if [ "$build" = 1 ]; then
  command -v uv >/dev/null 2>&1 || die "no uv: install it (https://docs.astral.sh/uv/), or --no-build with a venv already in $VENV"
  say "venv in $VENV"
  (cd "$repo/dashboard" && UV_PROJECT_ENVIRONMENT=$VENV UV_PYTHON_INSTALL_DIR=$OPT/python \
     uv sync --frozen --no-dev --no-install-project --quiet)
  command -v npm >/dev/null 2>&1 || die "no npm: install Node.js 20 or later, or --no-build with the bundle already in dashboard/web/dist"
  say "the page's bundle in dashboard/web/dist"
  (cd "$repo/dashboard/web" && npm ci --no-audit --no-fund --loglevel=error && npm run build --silent)
fi
[ -x "$VENV/bin/python" ] || die "no venv at $VENV: run this without --no-build"
[ -f "$repo/dashboard/web/dist/index.html" ] || die "no bundle in $repo/dashboard/web/dist: run this without --no-build"

# ---- settings
umask 027
render() {     # TEMPLATE: the template with this install's names filled in
  sed -e "s#@REPO@#$repo#g" -e "s#@VENV@#$VENV#g" -e "s#@ETC@#$ETC#g" \
      -e "s#@STATE@#$STATE#g" -e "s#@USER@#$SERVICE_USER#g" -e "s#@PULL_USER@#$PULL_USER#g" \
      -e "s#@GROUP@#$group#g" -e "s#@MEMORY_MAX@#$MEMORY_MAX#g" "$1"
}
if [ ! -e "$ETC/dashboard.env" ]; then
  say "$ETC/dashboard.env: your settings"
  render "$here/dashboard.env.example" > "$ETC/dashboard.env"
fi
chown root:"$SERVICE_USER" "$ETC/dashboard.env"
chmod 640 "$ETC/dashboard.env"

pulling=0
if [ "$mode" = fleet ]; then
  if [ ! -e "$ETC/fleet.env" ]; then
    say "$ETC/fleet.env: the nodes to pull"
    render "$repo/deploy/fleet/fleet.env.example" > "$ETC/fleet.env"
  fi
  chown root:"$SERVICE_USER" "$ETC/fleet.env"
  chmod 640 "$ETC/fleet.env"
  if [ ! -e "$ETC/pull_key" ]; then
    say "$ETC/pull_key: the key pull.py logs in to the nodes with"
    ssh-keygen -q -t ed25519 -N '' -C "rc-pull@$(hostname -s)" -f "$ETC/pull_key"
  fi
  chown "$SERVICE_USER:$SERVICE_USER" "$ETC/pull_key" "$ETC/pull_key.pub"
  chmod 600 "$ETC/pull_key"
  chmod 644 "$ETC/pull_key.pub"
  if cmp -s "$ETC/fleet.env" <(render "$repo/deploy/fleet/fleet.env.example"); then
    roots="RC_DASH_EBPF_ROOTS=$STATE/mirror/ebpf
RC_DASH_NODE_ROOTS=$STATE/mirror/node"
  else
    roots=$("$VENV/bin/python" "$repo/deploy/fleet/pull.py" --config "$ETC/fleet.env" roots) \
      || die "$ETC/fleet.env: see the message above"
    pulling=1
  fi
else
  roots="RC_DASH_EBPF_ROOTS=$EBPF_DIR
RC_DASH_NODE_ROOTS=$NODE_DIR"
fi
{
  echo "# Written by deploy/common/install-dashboard.sh ($mode) on every run; your settings"
  echo "# are in dashboard.env. Where the dashboard reads, and where it keeps things:"
  echo "$roots"
  echo "RC_DASH_STATIC_DIR=$repo/dashboard/web/dist"
  echo "RC_DASH_CACHE_DIR=$STATE/cache"
} > "$ETC/paths.env"
chown root:"$SERVICE_USER" "$ETC/paths.env"
chmod 640 "$ETC/paths.env"

# ---- units
umask 022
say "units in $UNIT_DIR"
render "$here/rc-dashboard.service.in" > "$UNIT_DIR/rc-dashboard.service"
if [ "$mode" = fleet ]; then
  render "$repo/deploy/fleet/rc-pull.service.in" > "$UNIT_DIR/rc-pull.service"
fi
systemctl daemon-reload
systemctl enable rc-dashboard.service >/dev/null 2>&1

if [ "$mode" = fleet ]; then
  systemctl enable rc-pull.service >/dev/null 2>&1
  if [ "$pulling" = 1 ]; then
    if systemctl is-active --quiet rc-pull.service; then
      systemctl restart rc-pull.service
    else
      systemctl start rc-pull.service
    fi
    say "rc-pull runs: $VENV/bin/python $repo/deploy/fleet/pull.py --config $ETC/fleet.env status"
  else
    say "rc-pull waits for your nodes: name them in $ETC/fleet.env, give each this key"
    say "(deploy/fleet/README.md, \"The nodes\"), and run this again:"
    cat "$ETC/pull_key.pub"
  fi
fi

set_in() { grep -Eq "^$1=.+" "$ETC/dashboard.env"; }
if set_in RC_DASH_PASSWORD_HASH || grep -Eiq '^RC_DASH_OPEN=(1|true|yes|on)$' "$ETC/dashboard.env"; then
  if systemctl is-active --quiet rc-dashboard.service; then
    say "restarting the dashboard: it refills its history from the feeds"
    systemctl restart rc-dashboard.service
  else
    systemctl start rc-dashboard.service
  fi
  say "the dashboard runs: systemctl status rc-dashboard"
else
  say "the dashboard starts once $ETC/dashboard.env has a password hash. Make one with"
  echo "      cd $repo/dashboard && $VENV/bin/python -m rc_dashboard new-password"
  echo "    put the hash it prints in RC_DASH_PASSWORD_HASH, then: systemctl start rc-dashboard"
fi
