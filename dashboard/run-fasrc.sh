#!/usr/bin/env bash
#
# run-fasrc.sh -- a second rc_dashboard, over the FAS RC capture, on :8085.
#
# The capture is boslogin06's eBPF tier, copied to this cluster as one
# `.jsonl.gz` per stream per day; rc_dashboard/gz.py reads it in place. It has
# no node tier, and it is days old, so the service's clock is pinned to its
# newest record (rc_dashboard/clock.py) -- on the wall clock every window would
# end in days of nothing and the default 24 h view would be blank.
#
# Hot reload exactly as `./run.sh build` (the :8086 instance): the backend
# restarts on every .py edit, and the bundle is rebuilt on every frontend edit
# (refresh the page). It is built into its OWN directory: :8086's watcher owns
# web/dist and empties it on every rebuild.
#
#   ./run-fasrc.sh          build mode, http://127.0.0.1:8085
#   ./run-fasrc.sh api      backend only, serving whatever the bundle dir holds
#
# FASRC_ROOT points it at another capture laid out <root>/<YYYY-MM-DD>/<host>.*.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export RC_DASH_EBPF_ROOTS="${FASRC_ROOT:-/scratch/marthen/fas-rc-test-data}"
# No node tier was shipped. Name a path that does not exist, so that feed reads
# "missing" and says where it looked: left unset or empty, it would fall back
# to the default candidates and could show THIS cluster's node tier beside
# boslogin06's processes.
export RC_DASH_NODE_ROOTS="$RC_DASH_EBPF_ROOTS/node"
export RC_DASH_AS_OF="${RC_DASH_AS_OF:-auto}"
# What counts as risky on FASRC login nodes (risk-profiles/, reducers/risk.py).
export RC_DASH_RISK_PROFILE="${RC_DASH_RISK_PROFILE:-risk-profiles/fasrc.json}"
# Named on every page, so it is never mistaken for the live dev-cluster one (run-dev.sh).
export RC_DASH_SITE_NAME="${RC_DASH_SITE_NAME:-FAS RC · boslogin06 capture · frozen}"
# Names go out as they are (user, 2026-10-13), though the tunnel serves this
# without a login. RC_DASH_ANONYMIZE=1 aliases every name (rc_dashboard/anonymize.py).
export RC_DASH_ANONYMIZE="${RC_DASH_ANONYMIZE:-0}"
export RC_DASH_PORT="${RC_DASH_PORT:-8085}"
# Its own saved window builds: they are keyed on the feed anyway, but both
# instances sharing one store would evict each other's (panels.cache_keep).
export RC_DASH_CACHE_DIR="${RC_DASH_CACHE_DIR:-$HERE/.cache/fasrc}"
export RC_DASH_STATIC_DIR="${RC_DASH_STATIC_DIR:-$HERE/.cache/fasrc/dist}"

exec "$HERE/run.sh" "${1:-build}"
