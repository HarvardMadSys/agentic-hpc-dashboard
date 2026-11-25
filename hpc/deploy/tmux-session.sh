#!/bin/bash
# tmux-session.sh: rc-tmux.service's ExecStart. Starts the rc-dashboard tmux server, on
# tmux's own defaults (-f /dev/null, so Ctrl-b is the prefix), with one window per
# process, each running keep-running.sh. A window whose loop ended stays open
# (remain-on-exit) to show why, and the hourly check starts it again.
#   tmux -L rc-dashboard attach      to watch; Ctrl-b d leaves it running
set -eu
keep=$(dirname "$0")/keep-running.sh
exec tmux -L rc-dashboard -f /dev/null \
  new-session -d -s rc-dashboard -n copy "exec $keep copy" \; \
  set-window-option -g remain-on-exit on \; \
  new-window -d -t rc-dashboard: -n dashboard "exec $keep dashboard"
