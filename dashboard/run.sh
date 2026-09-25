#!/usr/bin/env bash
#
# run.sh -- start rc_dashboard with hot-reload on both halves of the stack.
#
# The backend is Python, so `watchfiles` just restarts uvicorn on .py edits.
# The frontend is TypeScript/React and has to be compiled before a browser can
# run it, so there are two ways to keep it fresh, and this script offers both:
#
#   ./run.sh            DEV (default). Do not compile at all. Vite's dev server
#                       serves src/ straight from memory and pushes hot module
#                       replacement over its own socket; /api and /ws are
#                       proxied through to uvicorn. Edits land in the open page
#                       in well under a second and React state survives them.
#                       Open $RC_DASH_UI_PORT, NOT $RC_DASH_PORT. The browser
#                       never talks to uvicorn directly -- Vite proxies /api
#                       and /ws to it server-side -- so this still only needs
#                       ONE forwarded port off the login node, just the UI one:
#                         ssh -L 5173:127.0.0.1:5173 <login-node>
#
#   ./run.sh build      Serve the REAL bundle. `vite build --watch`
#                       incrementally recompiles web/dist/ on every edit and
#                       uvicorn keeps serving that directory, so everything is
#                       on $RC_DASH_PORT. Slower (a full bundle per edit) and
#                       the browser needs a manual refresh: there is no HMR
#                       channel. Use it to check what actually ships -- chunk
#                       splitting, `base: './'` asset URLs, StaticFiles -- none
#                       of which dev mode exercises.
#
#   ./run.sh api        Backend only, no frontend watcher.
#
# Requires: uv, node/npm. `watchfiles` is pulled in on the fly by
# `uv run --with watchfiles`, so it need not be a project dependency.

set -euo pipefail
set -m   # give every background job its own process group, so cleanup can
         # kill the whole uv->watchfiles->python and npm->vite trees at once.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB="$HERE/web"

MODE="${1:-dev}"
case "$MODE" in
  dev|build|api) ;;
  *) echo "usage: $0 [dev|build|api]" >&2; exit 2 ;;
esac

# ---- Environment -----------------------------------------------------
export RC_DASH_EBPF_ROOTS=/scratch/marthen/ebpfm/ebpf
export RC_DASH_NODE_ROOTS=/scratch/marthen/ebpfm/login
export RC_DASH_STATE_DIR=/scratch/marthen/ebpfm/dashstate
export RC_DASH_PORT="${RC_DASH_PORT:-8124}"
export RC_DASH_STATIC_DIR="${RC_DASH_STATIC_DIR:-$WEB/dist}"

# Vite dev server (dev mode only). Set RC_DASH_UI_HOST=0.0.0.0 to reach it from
# another machine instead of through a tunnel.
RC_DASH_UI_PORT="${RC_DASH_UI_PORT:-5173}"
RC_DASH_UI_HOST="${RC_DASH_UI_HOST:-127.0.0.1}"

# ---- Backend watch paths ---------------------------------------------
# Only source trees belong here. The ebpf/node data roots used to be on this
# list; they must not be. Under the old --filter python a new .jsonl there
# could never trigger a restart, so it only bought thousands of inotify watches
# on a high-churn data dir -- and under --filter default it is worse, because
# every incoming record would now bounce the server.
WATCH_PATHS=("$HERE/rc_dashboard")

# .py is not the only thing worth restarting on: the config file changes the
# app that create_app() builds, so watch it too when it exists.
for cfg in "$HERE/dashboard.config.json" "${RC_DASH_CONFIG:-}"; do
  [ -n "$cfg" ] && [ -f "$cfg" ] && WATCH_PATHS+=("$cfg")
done

backend() {
  # --filter default, NOT --filter python: the watch roots are already just
  # source, and PythonFilter passes only .py* -- under it the config file above
  # would be watched and then silently filtered out, never restarting anything.
  # DefaultFilter still drops __pycache__, *.pyc and editor swap files.
  exec uv run --with watchfiles watchfiles \
    --filter default \
    "uv run python -m rc_dashboard serve" \
    "${WATCH_PATHS[@]}"
}

frontend_dev() {
  # --mode real turns off the mock API plugin and turns on the proxy; the
  # target comes from RC_BACKEND, which must match the port uvicorn is on.
  cd "$WEB"
  RC_BACKEND="http://127.0.0.1:$RC_DASH_PORT" \
    exec npm run dev:real -- \
      --host "$RC_DASH_UI_HOST" --port "$RC_DASH_UI_PORT" --strictPort
}

frontend_build_watch() {
  cd "$WEB"
  exec npm run build:watch
}

# ---- Preflight -------------------------------------------------------
if [ "$MODE" != "api" ]; then
  command -v npm >/dev/null || { echo "npm not found; use './run.sh api'" >&2; exit 1; }
  [ -d "$WEB/node_modules" ] || { echo "==> npm ci"; (cd "$WEB" && npm ci); }
fi

# In build mode uvicorn mounts web/dist at import time, so it has to exist
# before the backend starts -- vite --watch would only create it a few seconds
# in, and the mount would silently fall back to the 503 "frontend not built".
if [ "$MODE" = "build" ] && [ ! -f "$WEB/dist/index.html" ]; then
  echo "==> initial build (dist/ is empty)"
  (cd "$WEB" && npm run build)
fi

# ---- Supervise -------------------------------------------------------
PIDS=()
cleanup() {
  trap - EXIT INT TERM
  [ ${#PIDS[@]} -eq 0 ] && return 0
  for pid in "${PIDS[@]}"; do
    kill -TERM "-$pid" 2>/dev/null || true   # negative pid == process group
  done
  # Then insist. watchfiles catches SIGTERM and has been seen to hang instead
  # of reaping the server it spawned, which leaves uvicorn holding
  # $RC_DASH_PORT and makes the next ./run.sh die on "address already in use".
  for _ in $(seq 20); do
    local alive=0
    for pid in "${PIDS[@]}"; do kill -0 "-$pid" 2>/dev/null && alive=1; done
    [ "$alive" -eq 0 ] && break
    sleep 0.25
  done
  for pid in "${PIDS[@]}"; do
    kill -KILL "-$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
# NOTE: bash can only trap INT if INT was not already SIG_IGN on entry, and a
# non-interactive shell sets SIG_IGN on everything it starts with `&`. Ctrl-C
# from a terminal is therefore fine, but `./run.sh &` from a script is not --
# send such a run SIGTERM, not SIGINT, to get a clean teardown.
trap cleanup EXIT INT TERM

echo "backend   http://127.0.0.1:$RC_DASH_PORT  (reload on .py under rc_dashboard/)"
case "$MODE" in
  dev)
    echo "frontend  http://$RC_DASH_UI_HOST:$RC_DASH_UI_PORT  <-- open this one (HMR, no compile)"
    ;;
  build)
    echo "frontend  rebuilt into web/dist/ on edit; refresh the backend URL by hand"
    echo "          (no type checking in --watch; run 'npm run typecheck' separately)"
    ;;
  api)
    echo "frontend  not running; serving whatever is in web/dist/"
    ;;
esac
echo

backend & PIDS+=($!)
case "$MODE" in
  dev)   frontend_dev & PIDS+=($!) ;;
  build) frontend_build_watch & PIDS+=($!) ;;
esac

# If either half dies, tear the other one down rather than leaving a half-up
# stack that looks alive but serves stale or unreachable content.
wait -n
