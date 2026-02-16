#!/usr/bin/env bash

set -u

MARKER="# ALPINN_BOT_AUTOSTART"
START_SCRIPT="$HOME/alpinn.ch_discord_bot/Auto_RUN_Alpinn_Bot_GIT_Update.sh"
ENTRY="@reboot /bin/bash \"$START_SCRIPT\" $MARKER"

current_crontab() {
  crontab -l 2>/dev/null || true
}

is_enabled() {
  current_crontab | grep -F "$MARKER" >/dev/null 2>&1
}

enable_autostart() {
  if is_enabled; then
    echo "Autostart already enabled"
    return 0
  fi

  {
    current_crontab
    echo "$ENTRY"
  } | crontab - || return 1

  echo "Autostart enabled"
}

disable_autostart() {
  local tmp_file
  tmp_file="$(mktemp)" || return 1

  current_crontab | grep -F -v "$MARKER" >"$tmp_file" || true
  crontab "$tmp_file" || {
    rm -f "$tmp_file"
    return 1
  }
  rm -f "$tmp_file"
  echo "Autostart disabled"
}

show_status() {
  if is_enabled; then
    echo "Autostart enabled"
  else
    echo "Autostart disabled"
  fi
}

main() {
  local action="${1:-status}"

  case "$action" in
    on)
      enable_autostart
      ;;
    off)
      disable_autostart
      ;;
    status)
      show_status
      ;;
    *)
      echo "Usage: $0 {on|off|status}" >&2
      return 2
      ;;
  esac
}

main "$@"
