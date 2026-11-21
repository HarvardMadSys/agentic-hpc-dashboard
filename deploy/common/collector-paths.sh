# shellcheck shell=bash
# collector-paths.sh: sourced by the installers, after DEFAULTS and die() are set.
# collector_paths sets EBPF_DIR and NODE_DIR to the folders the collector's units write.
#
# /etc/default/ebpfm may name them (EBPFM_OUTPUT_DIR, EBPFM_NODE_OUTPUT_DIR), and systemd
# lets that EnvironmentFile= override the units' own Environment=, so a folder named there
# is where the collector writes. Otherwise EBPF_DIR and NODE_DIR from the environment, else
# /var/log/ebpfm and /var/log/ebpfm-login. A folder the file and the environment name
# differently is refused: the dashboard would read one, and the collector write the other.

# KEY's last active value in $DEFAULTS, without the quotes systemd would remove
_ebpfm_default() {
  local v
  v=$( (sed -n "s/^$1=//p" "$DEFAULTS" 2>/dev/null || true) | tail -n 1)
  case $v in
    \"*\") v=${v#\"}; v=${v%\"} ;;
    \'*\') v=${v#\'}; v=${v%\'} ;;
  esac
  printf '%s' "$v"
}

collector_paths() {
  local named
  named=$(_ebpfm_default EBPFM_OUTPUT_DIR)
  if [ -n "$named" ] && [ -n "${EBPF_DIR:-}" ] && [ "$EBPF_DIR" != "$named" ]; then
    die "$DEFAULTS sets EBPFM_OUTPUT_DIR=$named, and EBPF_DIR says $EBPF_DIR; settle on one"
  fi
  EBPF_DIR=${named:-${EBPF_DIR:-/var/log/ebpfm}}
  named=$(_ebpfm_default EBPFM_NODE_OUTPUT_DIR)
  if [ -n "$named" ] && [ -n "${NODE_DIR:-}" ] && [ "$NODE_DIR" != "$named" ]; then
    die "$DEFAULTS sets EBPFM_NODE_OUTPUT_DIR=$named, and NODE_DIR says $NODE_DIR; settle on one"
  fi
  NODE_DIR=${named:-${NODE_DIR:-/var/log/ebpfm-login}}
}
