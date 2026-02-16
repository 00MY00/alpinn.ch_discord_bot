#!/usr/bin/env bash

####################################
#   Alpinn Auto News Discord BOT   #
####################################

set -u

BASE_DIR="$HOME/alpinn.ch_discord_bot"
PULL_DIR="$BASE_DIR/pull"
PROD_DIR="$BASE_DIR/prod"
LOG_FILE="$BASE_DIR/update.log"
LOCK_DIR="$BASE_DIR/.bot_runner.lock"
VENV_DIR="$PROD_DIR/.venv"
REPO_URL="https://github.com/00MY00/alpinn.ch_discord_bot"
BRANCH="main"
REBOOT_EXIT_CODE=42

log() {
  local level="$1"
  shift
  local msg="$*"
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  mkdir -p "$BASE_DIR"
  printf '[%s] [%s] %s\n' "$ts" "$level" "$msg" | tee -a "$LOG_FILE"
}

fail() {
  log "ERROR" "$*"
  exit 1
}

acquire_lock() {
  mkdir -p "$BASE_DIR"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$LOCK_DIR/pid"
    trap 'rm -rf "$LOCK_DIR"' EXIT
    return 0
  fi

  if [ -f "$LOCK_DIR/pid" ]; then
    log "INFO" "Script deja actif (pid: $(cat "$LOCK_DIR/pid" 2>/dev/null)). Aucune action."
  else
    log "INFO" "Script deja actif. Aucune action."
  fi
  return 1
}

test_git() {
  command -v git >/dev/null 2>&1
}

test_python() {
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
    BOT_PYTHON_BIN="$PYTHON_BIN"
    return 0
  fi

  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
    BOT_PYTHON_BIN="$PYTHON_BIN"
    return 0
  fi

  return 1
}

prepare_first_run() {
  mkdir -p "$BASE_DIR" "$PULL_DIR" "$PROD_DIR"

  if [ ! -d "$PULL_DIR/.git" ]; then
    log "INFO" "Initialisation du dossier pull via clone Git"
    rm -rf "$PULL_DIR"
    git clone --branch "$BRANCH" "$REPO_URL" "$PULL_DIR" || return 1
  else
    git -C "$PULL_DIR" remote set-url origin "$REPO_URL" || return 1
  fi

  if [ ! -f "$PROD_DIR/.env" ]; then
    cat > "$PROD_DIR/.env" <<'ENVEOF'
DISCORD_BOT_TOKEN=
ALPINN_API_KEY=
ENVEOF
    log "WARN" "Fichier $PROD_DIR/.env cree avec valeurs vides"
  fi
}

update_pull_repo_if_needed() {
  local nb=0

  git -C "$PULL_DIR" fetch origin "$BRANCH" >/dev/null 2>&1 || return 1
  nb="$(git -C "$PULL_DIR" rev-list --count "HEAD..origin/$BRANCH")" || return 1

  if [ "$nb" -gt 0 ]; then
    log "INFO" "Mise a jour detectee sur GitHub ($nb commit(s))"
    git -C "$PULL_DIR" reset --hard "origin/$BRANCH" >/dev/null 2>&1 || return 1
    echo "OK"
  else
    log "INFO" "Aucune mise a jour distante detectee"
    echo "NO"
  fi
}

sync_pull_to_prod() {
  local prod_env="$PROD_DIR/.env"
  local env_backup="$BASE_DIR/.env.backup"

  if [ -f "$prod_env" ]; then
    cp "$prod_env" "$env_backup" || return 1
  fi

  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude='.git' --exclude='.venv' "$PULL_DIR/" "$PROD_DIR/" || return 1
  else
    log "WARN" "rsync absent, copie via cp"
    rm -rf "$PROD_DIR"/*
    cp -a "$PULL_DIR/." "$PROD_DIR/" || return 1
    rm -rf "$PROD_DIR/.git"
  fi

  if [ -f "$env_backup" ]; then
    mv -f "$env_backup" "$prod_env" || return 1
  fi

  log "INFO" "Synchronisation pull -> prod terminee"
}

ensure_venv() {
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    log "INFO" "Creation environnement virtuel Python ($VENV_DIR)"
    "$PYTHON_BIN" -m venv "$VENV_DIR" >>"$LOG_FILE" 2>&1 || {
      log "ERROR" "Creation venv echouee. Installe python3-venv/python3-full puis relance."
      return 1
    }
  fi

  BOT_PYTHON_BIN="$VENV_DIR/bin/python"
  "$BOT_PYTHON_BIN" -m pip --version >/dev/null 2>&1 || {
    log "ERROR" "pip indisponible dans le venv: $VENV_DIR"
    return 1
  }
}

install_requirements_if_needed() {
  local req_file="$PROD_DIR/requirements.txt"

  [ -f "$req_file" ] || fail "requirements.txt introuvable dans $PROD_DIR"

  ensure_venv || return 1

  log "INFO" "Verification/installation des requirements (venv)"
  "$BOT_PYTHON_BIN" -m pip install -r "$req_file" >>"$LOG_FILE" 2>&1 || return 1
}

check_env_keys() {
  local env_file="$PROD_DIR/.env"
  local token=""
  local api_key=""

  [ -f "$env_file" ] || { log "ERROR" "Fichier .env manquant: $env_file"; return 1; }

  token="$(grep -E '^DISCORD_BOT_TOKEN=' "$env_file" | tail -n1 | cut -d'=' -f2-)"
  api_key="$(grep -E '^ALPINN_API_KEY=' "$env_file" | tail -n1 | cut -d'=' -f2-)"

  if [ -z "$token" ]; then
    log "ERROR" "DISCORD_BOT_TOKEN est vide dans $env_file"
    return 1
  fi

  if [ -z "$api_key" ]; then
    log "ERROR" "ALPINN_API_KEY est vide dans $env_file"
    return 1
  fi

  return 0
}

run_bot() {
  cd "$PROD_DIR" || return 1
  log "INFO" "Demarrage du bot (bot.py)"
  "$BOT_PYTHON_BIN" bot.py
}

full_update_cycle() {
  update_pull_repo_if_needed >/dev/null || return 1
  sync_pull_to_prod || return 1
  install_requirements_if_needed || return 1
  check_env_keys || return 1
}

supervise_bot() {
  while true; do
    run_bot
    exit_code=$?

    if [ "$exit_code" -eq "$REBOOT_EXIT_CODE" ]; then
      log "INFO" "Reboot demande par le bot: mise a jour puis relance"
      full_update_cycle || fail "Cycle de mise a jour apres reboot echoue"
      continue
    fi

    if [ "$exit_code" -eq 0 ]; then
      log "INFO" "Bot arrete proprement (code 0). Fin du script."
      break
    fi

    log "WARN" "Bot arrete avec code $exit_code. Nouvelle tentative dans 10s."
    sleep 10
  done
}

main() {
  log "INFO" "Debut execution script update/lancement"
  acquire_lock || exit 0

  test_git || fail "Git n'est pas installe"
  test_python || fail "Python n'est pas installe"

  prepare_first_run || fail "Preparation initiale echouee"

  full_update_cycle || fail "Cycle initial update/sync/install/config echoue"
  supervise_bot
}

main "$@"
