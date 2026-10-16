#!/bin/bash
# Container entrypoint for the ebpfm collector.
#
# Why not just run ebpfm.sh: `ebpfm.sh start` launches the eBPF tier through
# `systemd-run --slice=system.slice` to escape the login-node user-slice CPU cap.
# Inside a container there is no host systemd to talk to and no capped user
# slice to escape, so that path is both unavailable and unnecessary. We instead
# run the tracer in the FOREGROUND as PID 1's child, which is exactly what a
# container runtime wants to supervise and what makes `docker stop` deliver the
# SIGTERM the collector turns into its `truncated` flush.
#
# Everything else in ebpfm.sh (check, features, bootstrap, the node tier) is
# still reachable by passing it as an argument, e.g.:
#   docker run ... ebpfm check
#   docker run ... ebpfm features
#
# Default (no args, or `collector`): run the eBPF tracer in the foreground.
set -euo pipefail

EBPFM_DIR=/opt/ebpfm
export EBPFM_OUTPUT_DIR="${EBPFM_OUTPUT_DIR:-/var/log/ebpfm}"
export EBPFM_NODE_OUTPUT_DIR="${EBPFM_NODE_OUTPUT_DIR:-/var/log/ebpfm-login}"

mkdir -p "$EBPFM_OUTPUT_DIR" "$EBPFM_NODE_OUTPUT_DIR"

case "${1:-collector}" in
  collector|"")
    # Foreground eBPF tracer. Hostname arg lets the operator override the
    # container hostname with the real node name (recommended: --hostname $(hostname)
    # or -e so the day files are named for the host, not the container id).
    exec python3 "$EBPFM_DIR/ebpf_trace.py" "${EBPFM_HOST:-$(hostname -s)}"
    ;;
  ebpfm)
    # Pass the remaining args straight to the bundle entrypoint.
    shift
    exec "$EBPFM_DIR/ebpfm.sh" "$@"
    ;;
  *)
    # Anything else: treat as a raw command so the image stays debuggable
    # (e.g. `docker run ... bash`).
    exec "$@"
    ;;
esac
