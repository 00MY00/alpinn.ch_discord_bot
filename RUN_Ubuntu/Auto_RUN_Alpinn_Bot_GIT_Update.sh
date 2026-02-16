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
DEPLOY_STAMP_FILE="$PROD_DIR/.deploy_pull_head"
REQ_STAMP_FILE="$PROD_DIR/.venv/.requirements.sha256"
UPDATE_DELAY_FILE="$BASE_DIR/.update_poll_minutes"
REPO_URL="https://github.com/00MY00/alpinn.ch_discord_bot"
BRANCH="main"
REBOOT_EXIT_CODE=42
DEFAULT_UPDATE_POLL_SECONDS=60
UPDATE_POLL_SECONDS="$DEFAULT_UPDATE_POLL_SECONDS"
LAST_UPDATE_POLL_SECONDS=0
DEPLOY_UPDATED=0
BOT_PID=0

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

current_pull_head() {
  git -C "$PULL_DIR" rev-parse HEAD 2>/dev/null
}

current_prod_head() {
  if [ -f "$DEPLOY_STAMP_FILE" ]; then
    cat "$DEPLOY_STAMP_FILE"
    return 0
  fi
  return 1
}

load_update_poll_seconds() {
  local raw=""
  local minutes=0
  UPDATE_POLL_SECONDS="$DEFAULT_UPDATE_POLL_SECONDS"

  if [ -f "$UPDATE_DELAY_FILE" ]; then
    raw="$(tr -d '[:space:]' < "$UPDATE_DELAY_FILE" 2>/dev/null || true)"
    if [[ "$raw" =~ ^[0-9]+$ ]]; then
      minutes="$raw"
      if [ "$minutes" -ge 1 ]; then
        UPDATE_POLL_SECONDS=$((minutes * 60))
      fi
    fi
  fi

  if [ "$LAST_UPDATE_POLL_SECONDS" -ne "$UPDATE_POLL_SECONDS" ]; then
    log "INFO" "Delai verification update: $((UPDATE_POLL_SECONDS / 60)) minute(s)"
    LAST_UPDATE_POLL_SECONDS="$UPDATE_POLL_SECONDS"
  fi
}

sync_pull_to_prod_if_needed() {
  local prod_env="$PROD_DIR/.env"
  local env_backup="$BASE_DIR/.env.backup"
  local pull_head=""
  local prod_head=""

  DEPLOY_UPDATED=0
  pull_head="$(current_pull_head)" || return 1
  prod_head="$(current_prod_head || true)"

  if [ "$pull_head" = "$prod_head" ]; then
    log "INFO" "Prod deja a jour (meme revision que pull)"
    return 0
  fi

  log "INFO" "Nouvelle revision detectee: deploiement pull -> prod"

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

  echo "$pull_head" > "$DEPLOY_STAMP_FILE" || return 1
  DEPLOY_UPDATED=1
  log "INFO" "Synchronisation pull -> prod terminee ($pull_head)"
}

ensure_venv() {
  local created=0

  if [ ! -x "$VENV_DIR/bin/python" ]; then
    log "INFO" "Creation environnement virtuel Python ($VENV_DIR)"
    "$PYTHON_BIN" -m venv "$VENV_DIR" >>"$LOG_FILE" 2>&1 || {
      log "ERROR" "Creation venv echouee. Installe python3-venv/python3-full puis relance."
      return 1
    }
    created=1
  fi

  BOT_PYTHON_BIN="$VENV_DIR/bin/python"
  "$BOT_PYTHON_BIN" -m pip --version >/dev/null 2>&1 || {
    log "WARN" "pip indisponible dans le venv, recreation de $VENV_DIR"
    rm -rf "$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR" >>"$LOG_FILE" 2>&1 || {
      log "ERROR" "Recreation venv echouee. Installe python3-venv/python3-full puis relance."
      return 1
    }
    BOT_PYTHON_BIN="$VENV_DIR/bin/python"
    "$BOT_PYTHON_BIN" -m pip --version >/dev/null 2>&1 || {
      log "ERROR" "pip indisponible apres recreation du venv: $VENV_DIR"
      return 1
    }
    created=1
  }

  if [ "$created" -eq 1 ]; then
    rm -f "$REQ_STAMP_FILE"
  fi
}

requirements_fingerprint() {
  local req_file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$req_file" | awk '{print $1}'
    return 0
  fi
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$req_file" | awk '{print $1}'
    return 0
  fi
  return 1
}

requirements_install_needed() {
  local req_hash="$1"
  local stamped_hash=""

  if [ ! -f "$REQ_STAMP_FILE" ]; then
    return 0
  fi

  stamped_hash="$(cat "$REQ_STAMP_FILE" 2>/dev/null || true)"
  [ "$req_hash" != "$stamped_hash" ]
}

install_requirements_if_needed() {
  local req_file="$PROD_DIR/requirements.txt"
  local force_install="${1:-0}"
  local req_hash=""

  [ -f "$req_file" ] || fail "requirements.txt introuvable dans $PROD_DIR"

  ensure_venv || return 1

  req_hash="$(requirements_fingerprint "$req_file")" || {
    log "ERROR" "Impossible de calculer le hash de requirements.txt (sha256sum/shasum manquant)"
    return 1
  }

  if [ "$force_install" -ne 1 ] && ! requirements_install_needed "$req_hash"; then
    log "INFO" "Requirements deja a jour dans le venv"
    return 0
  fi

  log "INFO" "Verification/installation des requirements (venv)"
  "$BOT_PYTHON_BIN" -m pip install -r "$req_file" >>"$LOG_FILE" 2>&1 || return 1
  echo "$req_hash" > "$REQ_STAMP_FILE" || return 1
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

start_bot() {
  cd "$PROD_DIR" || return 1
  log "INFO" "Demarrage du bot (bot.py)"
  "$BOT_PYTHON_BIN" bot.py &
  BOT_PID=$!
  log "INFO" "Bot demarre (pid: $BOT_PID)"
}

stop_bot() {
  if [ "$BOT_PID" -le 0 ]; then
    return 0
  fi
  if kill -0 "$BOT_PID" >/dev/null 2>&1; then
    log "INFO" "Arret du bot (pid: $BOT_PID)"
    kill "$BOT_PID" >/dev/null 2>&1 || true
    wait "$BOT_PID" 2>/dev/null || true
  fi
  BOT_PID=0
}

full_update_cycle() {
  update_pull_repo_if_needed >/dev/null || return 1
  sync_pull_to_prod_if_needed || return 1
  install_requirements_if_needed "$DEPLOY_UPDATED" || return 1
  check_env_keys || return 1
}

supervise_bot() {
  local exit_code=0

  start_bot || return 1

  while true; do
    if ! kill -0 "$BOT_PID" >/dev/null 2>&1; then
      wait "$BOT_PID" || exit_code=$?
      BOT_PID=0

      if [ "$exit_code" -eq "$REBOOT_EXIT_CODE" ]; then
        log "INFO" "Reboot demande par le bot: mise a jour puis relance"
        full_update_cycle || fail "Cycle de mise a jour apres reboot echoue"
        start_bot || fail "Relance bot apres reboot echouee"
        exit_code=0
        continue
      fi

      if [ "$exit_code" -eq 0 ]; then
        log "INFO" "Bot arrete proprement (code 0). Fin du script."
        break
      fi

      log "WARN" "Bot arrete avec code $exit_code. Nouvelle tentative dans 10s."
      sleep 10
      full_update_cycle || log "WARN" "Update cycle en echec avant relance bot"
      start_bot || fail "Relance bot apres crash echouee"
      exit_code=0
      continue
    fi

    load_update_poll_seconds
    sleep "$UPDATE_POLL_SECONDS"
    full_update_cycle || {
      log "WARN" "Verification de mise a jour echouee (on garde le bot en route)"
      continue
    }

    if [ "$DEPLOY_UPDATED" -eq 1 ]; then
      log "INFO" "Mise a jour deployee: redemarrage automatique du bot"
      stop_bot
      start_bot || fail "Relance bot apres update echouee"
    fi
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
