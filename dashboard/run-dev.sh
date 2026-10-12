#!/usr/bin/env bash
#
# run-dev.sh -- the dashboard over THIS cluster's live feed, on :8086.
#
# The dev cluster: rtx6000a, read from the collectors' output under
# /scratch/marthen/ebpfm (ebpf/ the root tier, login/ the node tier), on the
# wall clock -- unlike run-fasrc.sh, which serves a frozen capture from FAS RC
# on :8085. The two are told apart on every page by `site.name`.
#
# This host is a GPU box that is also the login node, so the risk rules come
# from risk-profiles/gpu-dev.json (nvidia-smi and nvcc are fine here), and it
# runs no Slurm, so the Slurm tab and everything reduced for it are off.
#
# Hot reload as ./run.sh build: the backend restarts on every .py edit, the
# bundle in web/dist is rebuilt on every frontend edit (refresh the page).
#
#   ./run-dev.sh          build mode, http://127.0.0.1:8086
#   ./run-dev.sh api      backend only

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEV_ROOT="${DEV_ROOT:-/scratch/marthen/ebpfm}"
export RC_DASH_EBPF_ROOTS="${RC_DASH_EBPF_ROOTS:-$DEV_ROOT/ebpf}"
export RC_DASH_NODE_ROOTS="${RC_DASH_NODE_ROOTS:-$DEV_ROOT/login}"
export RC_DASH_RISK_PROFILE="${RC_DASH_RISK_PROFILE:-risk-profiles/gpu-dev.json}"
export RC_DASH_SLURM="${RC_DASH_SLURM:-0}"
export RC_DASH_SITE_NAME="${RC_DASH_SITE_NAME:-dev cluster · rtx6000a · live}"
# Reachable without a login through the tunnel, so names go out as aliases
# (rc_dashboard/anonymize.py). RC_DASH_ANONYMIZE=0 shows the real ones.
export RC_DASH_ANONYMIZE="${RC_DASH_ANONYMIZE:-1}"
export RC_DASH_PORT="${RC_DASH_PORT:-8086}"

exec "$HERE/run.sh" "${1:-build}"
