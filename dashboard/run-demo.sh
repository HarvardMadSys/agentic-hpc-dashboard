#!/usr/bin/env bash
#
# run-demo.sh -- the dashboard over a SYNTHETIC feed it writes itself, on :8087.
#
# No cluster, no collector, no root: `rc_dashboard demo` writes a few days of
# made-up records for fictional login nodes and their users into
# .cache/demo/feed, keeps appending to it in real time, and serves the real
# service over it (rc_dashboard/demo/__init__.py). Every record carries
# "synthetic": true and every page says "synthetic data" -- use it to show the
# dashboard, never to report on one.
#
# Hot reload as ./run.sh build: the backend restarts on every .py edit and the
# demo resumes its feed where it stopped; the bundle is rebuilt into its OWN
# directory on every frontend edit (refresh the page).
#
#   ./run-demo.sh          build mode, http://127.0.0.1:8087
#   ./run-demo.sh dev      Vite dev server with HMR on :5173, backend on :8087
#   ./run-demo.sh api      backend only, serving whatever the bundle dir holds
#
# RC_DASH_DEMO_DAYS (3), RC_DASH_DEMO_HOSTS (2), RC_DASH_DEMO_SEED (demo) and
# RC_DASH_DEMO_DIR (.cache/demo) shape the feed; a different seed is a different
# set of sessions, and changing any of them writes the feed afresh.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export RC_DASH_COMMAND=demo
export RC_DASH_PORT="${RC_DASH_PORT:-8087}"
# its own bundle: run-dev.sh's watcher owns web/dist and empties it on every rebuild
export RC_DASH_STATIC_DIR="${RC_DASH_STATIC_DIR:-$HERE/.cache/demo/dist}"

exec "$HERE/run.sh" "${1:-build}"
