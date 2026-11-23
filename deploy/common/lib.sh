# shellcheck shell=bash
# lib.sh: sourced by the installers (install-collector.sh, install-dashboard.sh,
# install-node.sh) and the kit's other scripts, after `say` and `die` are set. What they
# share:
#
#   --help and --plan   show_help prints the calling script's header comment; with
#                       PLAN=1 (--plan), `run` and `put` print what they would do and
#                       change nothing, while every read still happens, so the plan is
#                       this host's.
#   install.env         the answers an installer was given (SERVICE_USER, ETC, STATE,
#                       OPT, MEMORY_MAX, EBPF_DIR, ...): read first on every run, the
#                       environment still winning, and written back with every change
#                       printed. It sits at /etc/rc-dashboard/install.env whatever ETC is
#                       (INSTALL_ENV moves it, for the tests), so a run that is not told
#                       ETC still finds it.
#   yes/no words        1/0, true/false, yes/no, on/off, in any case, as the dashboard
#                       takes them.

PLAN=${PLAN:-0}
INSTALL_ENV=${INSTALL_ENV:-/etc/rc-dashboard/install.env}

# The header comment of FILE, without its #s: what --help prints.
show_help() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$1"
}

# A command line as a person would type it again.
_quoted() {
  local out= a
  for a in "$@"; do out+=" $(printf '%q' "$a")"; done
  printf '%s' "${out# }"
}

# run CMD...: CMD, or with PLAN=1 the line saying it would run.
run() {
  if [ "$PLAN" = 1 ]; then
    printf '    would run: %s\n' "$(_quoted "$@")"
    return 0
  fi
  "$@"
}

# put FILE < CONTENT: write FILE (atomically, in its folder, keeping the mode and owner
# of the one it replaces), or with PLAN=1 say whether it would be new, changed (with the
# change), or the same.
put() {
  local f=$1 tmp
  if [ "$PLAN" = 1 ]; then
    tmp=$(mktemp)
    cat > "$tmp"
    if [ ! -e "$f" ]; then
      printf '    would write %s (new)\n' "$f"
    elif cmp -s "$tmp" "$f"; then
      printf '    %s stays as it is\n' "$f"
    else
      printf '    would change %s:\n' "$f"
      { diff -u "$f" "$tmp" 2>/dev/null || true; } | sed -n '3,$s/^/      /p' | head -n 40
    fi
    rm -f "$tmp"
    return 0
  fi
  tmp=$(mktemp "$(dirname "$f")/.$(basename "$f").XXXXXX")
  cat > "$tmp"
  if [ -e "$f" ] && cmp -s "$tmp" "$f"; then
    rm -f "$tmp"                  # the same: its inode and time stay
    return 0
  fi
  if [ -e "$f" ]; then
    chmod --reference="$f" "$tmp" 2>/dev/null || true
    chown --reference="$f" "$tmp" 2>/dev/null || true
  fi
  mv -f "$tmp" "$f"
}

# A yes/no word as 1 or 0; anything else is refused, naming KEY.
yesno() {   # KEY VALUE
  case $(printf '%s' "$2" | tr '[:upper:]' '[:lower:]') in
    1|true|yes|on) echo 1 ;;
    0|false|no|off) echo 0 ;;
    *) die "$1='$2' is not one of 1/0, true/false, yes/no, on/off" ;;
  esac
}

# KEY's last value in FILE, as systemd's EnvironmentFile= reads it: the last assignment
# wins, without the blanks or the one pair of quotes around it.
env_value() {   # FILE KEY
  local v
  v=$( (sed -n "s/^$2=//p" "$1" 2>/dev/null || true) | tail -n 1)
  v=$(printf '%s' "$v" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  case $v in
    \"*\") v=${v#\"}; v=${v%\"} ;;
    \'*\') v=${v#\'}; v=${v%\'} ;;
  esac
  printf '%s' "$v"
}

# install_env_load KEY...: each KEY the environment leaves unset (or empty) takes its
# value from install.env, if that names it. FROM_<KEY> says where it came from: env,
# install.env, or default (the caller's default, not set here).
install_env_load() {
  local k v
  for k in "$@"; do
    if [ -n "${!k:-}" ]; then
      printf -v "FROM_$k" '%s' env
    elif [ -f "$INSTALL_ENV" ] && v=$(env_value "$INSTALL_ENV" "$k") && [ -n "$v" ]; then
      printf -v "$k" '%s' "$v"
      printf -v "FROM_$k" '%s' install.env
    else
      printf -v "FROM_$k" '%s' default
    fi
  done
}

# install_env_save KEY...: write each KEY's value now into install.env, in its place, the
# keys other scripts saved there kept as they are, and print each one that changed.
install_env_save() {
  local k old line found
  for k in "$@"; do
    if [ -f "$INSTALL_ENV" ] && grep -q "^$k=" "$INSTALL_ENV"; then
      old=$(env_value "$INSTALL_ENV" "$k")
      [ "$old" = "${!k}" ] || say "$k: ${old:-(empty)} -> ${!k:-(empty)} (saved in $INSTALL_ENV)"
    fi
  done
  [ "$PLAN" = 1 ] || mkdir -p "$(dirname "$INSTALL_ENV")"
  {
    if [ -f "$INSTALL_ENV" ]; then
      while IFS= read -r line || [ -n "$line" ]; do
        found=
        for k in "$@"; do
          case $line in "$k="*) found=$k ;; esac
        done
        if [ -n "$found" ]; then printf '%s=%s\n' "$found" "${!found}"; else printf '%s\n' "$line"; fi
      done < "$INSTALL_ENV"
    else
      echo "# Written by deploy/common's installers: the answers they were given, read first"
      echo "# on every run, the environment winning. Change one by giving it again, or here."
    fi
    for k in "$@"; do
      [ -f "$INSTALL_ENV" ] && grep -q "^$k=" "$INSTALL_ENV" && continue
      printf '%s=%s\n' "$k" "${!k}"
    done
  } | put "$INSTALL_ENV"
  [ "$PLAN" = 1 ] || chmod 644 "$INSTALL_ENV"
}

# set_env_line FILE KEY VALUE: KEY's one active line in FILE says VALUE (in double
# quotes when it has a blank), where its first active line was, else at the end; every
# other line stays. Prints the change.
set_env_line() {
  local f=$1 key=$2 val=$3 line old
  case $val in *[\"\\\$\`]*) die "$key: a value with \", \\, \$ or \` cannot go in $f" ;; esac
  line="$key=$val"
  case $val in *[[:space:]]*) line="$key=\"$val\"" ;; esac
  old=$(env_value "$f" "$key")
  if [ "$old" = "$val" ] && [ "$(grep -c "^$key=" "$f")" -eq 1 ]; then
    return 0
  fi
  say "$key in ${SHOWN_AS:-$f}: ${old:-(empty)} -> $val"
  awk -v key="$key" -v line="$line" '
    index($0, key "=") == 1 { if (!done) { print line; done = 1 }; next }
    { print }
    END { if (!done) print line }' "$f" | put "$f"
}
