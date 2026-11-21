#!/bin/bash
# install-dashboard.sh [--no-build]: the fleet's dashboard host. The dashboard, and
# rc-pull.service, which copies every node's collector files here with rsync (pull.py).
# Run as root, from a checkout of this repository that any account can read. It is
# deploy/common/install-dashboard.sh fleet, which says what it makes; README.md says the
# order to run things in.
exec "$(cd "$(dirname "$0")/../common" && pwd)/install-dashboard.sh" fleet "$@"
