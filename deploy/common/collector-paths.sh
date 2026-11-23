# shellcheck shell=bash
# collector-paths.sh: sourced by the installers, after DEFAULTS and die() are set.
# collector_paths sets EBPF_DIR and NODE_DIR to the folders the collector's units write.
#
# /etc/default/ebpfm may name them (EBPFM_OUTPUT_DIR, EBPFM_NODE_OUTPUT_DIR), and systemd
# lets that EnvironmentFile= override the units' own Environment=, so a folder named there
# is where the collector writes; install-collector.sh writes both there, so every later
# run, and `ebpfm status`, finds them. Otherwise EBPF_DIR and NODE_DIR from the
# environment (or from install.env, an earlier run's answer), else the folder an installed
# unit names (one installed before the folders were saved in the defaults file, or the
# RPM's), else /var/log/ebpfm and /var/log/ebpfm-login. A folder named two different ways
# is refused: the dashboard would read one, and the collector write the other, and
# moving where the collector writes leaves its days behind.

# KEY's last active value in $DEFAULTS, as systemd reads it: the last assignment wins,
# without the whitespace around it, or the quotes around it
_ebpfm_default() {
  local v
  v=$( (sed -n "s/^$1=//p" "$DEFAULTS" 2>/dev/null || true) | tail -n 1)
  v=$(printf '%s' "$v" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  case $v in
    \"*\") v=${v#\"}; v=${v%\"} ;;
    \'*\') v=${v#\'}; v=${v%\'} ;;
  esac
  printf '%s' "$v"
}

_said_by() {     # where a value came from, for a refusal
  case $1 in
    install.env) printf 'from %s, an earlier run' "${INSTALL_ENV:-install.env}" ;;
    *) printf 'from the environment' ;;
  esac
}

# VAR KEY UNIT DEFAULT: one of the two folders.
_collector_path() {
  local var=$1 key=$2 unit=$3 def=$4 named given from u inunit= where=
  named=$(_ebpfm_default "$key")
  given=${!var:-}
  from=FROM_$var
  from=${!from:-env}
  if [ -n "$named" ]; then
    if [ -n "$given" ] && [ "$given" != "$named" ]; then
      die "$DEFAULTS sets $key=$named, and $var says $given ($(_said_by "$from")); settle on one"
    fi
    printf -v "$var" '%s' "$named"
    return 0
  fi
  for u in "${EBPFM_UNIT_DIR:-/etc/systemd/system}/$unit" \
           "${EBPFM_PACKAGED_UNIT:-/usr/lib/systemd/system/ebpfm.service}"; do
    case $u in */"$unit") ;; *) continue ;; esac
    inunit=$( (sed -n "s/^Environment=$key=//p" "$u" 2>/dev/null || true) | tail -n 1)
    if [ -n "$inunit" ]; then where=$u; break; fi
  done
  if [ -n "$inunit" ]; then
    if [ -n "$given" ] && [ "$given" != "$inunit" ]; then
      die "the collector writes $inunit ($where), and $var says $given ($(_said_by "$from")):
  that would leave its days behind. To move it, set $key in $DEFAULTS; else settle on one"
    fi
    printf -v "$var" '%s' "$inunit"
    return 0
  fi
  printf -v "$var" '%s' "${given:-$def}"
}

collector_paths() {
  _collector_path EBPF_DIR EBPFM_OUTPUT_DIR ebpfm.service /var/log/ebpfm
  _collector_path NODE_DIR EBPFM_NODE_OUTPUT_DIR ebpfm-node.service /var/log/ebpfm-login
}

# set_default KEY VALUE: KEY's one active line in $DEFAULTS says VALUE, in the place of
# its commented example if the file has one, else at the end; every other line stays.
# An empty assignment leaves a setting as it is, and systemd keeps the last one, so no
# other active line for KEY may stay to outlast this one.
set_default() {
  local key=$1 val=$2
  if [ "$(_ebpfm_default "$key")" = "$val" ] && [ "$(grep -c "^$key=" "$DEFAULTS")" -eq 1 ]; then
    return 0
  fi
  say "$key=$val in ${DEFAULTS_SHOWN:-$DEFAULTS}"
  if [ "${PLAN:-0}" = 1 ]; then
    return 0
  fi
  sed -i "/^$key=/d" "$DEFAULTS"
  if grep -q "^#$key=" "$DEFAULTS"; then
    sed -i "0,/^#$key=.*/s|^#$key=.*|$key=$val|" "$DEFAULTS"
  else
    printf '%s=%s\n' "$key" "$val" >> "$DEFAULTS"
  fi
}
