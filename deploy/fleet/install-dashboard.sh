#!/bin/bash
# install-dashboard.sh [--no-build] [--plan] [--help]: the fleet's dashboard host. The
# dashboard, and rc-pull.service, which copies every node's collector files here with
# rsync (pull.py). Run as root, from a checkout of this repository that any account can
# read. It is deploy/common/install-dashboard.sh fleet, which says what it makes and which
# settings it takes (`deploy/common/install-dashboard.sh --help`); README.md says the
# order to run things in. --plan prints what it would do and changes nothing.
case " $* " in
  *" -h "*|*" --help "*) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
esac
exec "$(cd "$(dirname "$0")/../common" && pwd)/install-dashboard.sh" fleet "$@"
