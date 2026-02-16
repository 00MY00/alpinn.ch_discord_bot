#!/usr/bin/env bash

set -u

SERVICE_NAME="alpinn-bot.service"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"
RUNNER_SCRIPT="/home/ubuntu/alpinn.ch_discord_bot/RUN_Ubuntu/Auto_RUN_Alpinn_Bot_GIT_Update.sh"
SERVICE_USER="ubuntu"
WORKDIR="/home/ubuntu/alpinn.ch_discord_bot"

usage() {
  cat <<'EOF'
Usage:
  ./RUN_Ubuntu/Manage_Systemd_Service.sh install
  ./RUN_Ubuntu/Manage_Systemd_Service.sh uninstall
  ./RUN_Ubuntu/Manage_Systemd_Service.sh start
  ./RUN_Ubuntu/Manage_Systemd_Service.sh stop
  ./RUN_Ubuntu/Manage_Systemd_Service.sh restart
  ./RUN_Ubuntu/Manage_Systemd_Service.sh status
  ./RUN_Ubuntu/Manage_Systemd_Service.sh logs

Optional env overrides:
  SERVICE_USER=ubuntu
  WORKDIR=/home/ubuntu/alpinn.ch_discord_bot
  RUNNER_SCRIPT=/home/ubuntu/alpinn.ch_discord_bot/RUN_Ubuntu/Auto_RUN_Alpinn_Bot_GIT_Update.sh
EOF
}

require_sudo() {
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo introuvable. Installe sudo ou execute les commandes root manuellement."
    exit 1
  fi
}

install_service() {
  require_sudo

  if [ ! -f "$RUNNER_SCRIPT" ]; then
    echo "Runner introuvable: $RUNNER_SCRIPT"
    exit 1
  fi

  cat <<EOF | sudo tee "$SERVICE_FILE" >/dev/null
[Unit]
Description=Alpinn Discord Bot Runner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${WORKDIR}
Environment=ALPINN_NO_DAEMONIZE=1
ExecStart=/bin/bash ${RUNNER_SCRIPT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable --now "$SERVICE_NAME"
  sudo systemctl status "$SERVICE_NAME" --no-pager
}

uninstall_service() {
  require_sudo
  sudo systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
  sudo rm -f "$SERVICE_FILE"
  sudo systemctl daemon-reload
  echo "Service supprime: $SERVICE_NAME"
}

main() {
  local action="${1:-}"

  case "$action" in
    install)
      install_service
      ;;
    uninstall)
      uninstall_service
      ;;
    start|stop|restart|status)
      require_sudo
      sudo systemctl "$action" "$SERVICE_NAME"
      ;;
    logs)
      require_sudo
      sudo journalctl -u "$SERVICE_NAME" -n 120 --no-pager
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
