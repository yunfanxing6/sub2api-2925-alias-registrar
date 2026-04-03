#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/root/sub2api-2925-alias-registrar}"
PYTHON_BIN="${PYTHON_BIN:-$REPO_DIR/.venv/bin/python}"
XVFB_RUN_BIN="${XVFB_RUN_BIN:-/usr/bin/xvfb-run}"

args=(
  "$REPO_DIR/sub2api_browser_tempmail_registrar.py"
  --sub2api-url "${SUB2API_URL:?SUB2API_URL is required}"
  --admin-api-key "${SUB2API_ADMIN_API_KEY:?SUB2API_ADMIN_API_KEY is required}"
  --group-ids "${GROUP_IDS:-all}"
  --concurrency "${CONCURRENCY:-10}"
  --priority "${PRIORITY:-1}"
  --count "${COUNT:-1}"
  --max-attempts "${MAX_ATTEMPTS:-1}"
  --retry-sleep "${RETRY_SLEEP:-1}"
  --sleep "${SLEEP_SECONDS:-90}"
  --history-file "${HISTORY_FILE:-$REPO_DIR/tempmail_history_service.jsonl}"
  --artifacts-dir "${ARTIFACTS_DIR:-$REPO_DIR/browser_artifacts}"
  --mail-sources "${MAIL_SOURCES:-tempmail_lol,mailtm,onesecmail}"
)

if [[ -n "${PROXY:-}" ]]; then
  args+=(--proxy "$PROXY")
fi

if [[ -n "${SUB2API_PROXY_ID:-}" ]]; then
  args+=(--sub2api-proxy-id "$SUB2API_PROXY_ID")
fi

if [[ -n "${REDIRECT_URI:-}" ]]; then
  args+=(--redirect-uri "$REDIRECT_URI")
fi

if [[ -n "${CHROMIUM_PATH:-}" ]]; then
  args+=(--chromium-path "$CHROMIUM_PATH")
fi

if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  args+=(--telegram-bot-token "$TELEGRAM_BOT_TOKEN")
fi

if [[ -n "${TELEGRAM_CHAT_ID:-}" ]]; then
  args+=(--telegram-chat-id "$TELEGRAM_CHAT_ID")
fi

if [[ -n "${TELEGRAM_CHAT_CACHE_FILE:-}" ]]; then
  args+=(--telegram-chat-cache-file "$TELEGRAM_CHAT_CACHE_FILE")
fi

if [[ -n "${DUCKMAIL_KEY:-}" ]]; then
  args+=(--duckmail-key "$DUCKMAIL_KEY")
fi

if [[ "${DEBUG:-0}" == "1" ]]; then
  args+=(--debug)
fi

if [[ "${HEADLESS:-0}" == "1" ]]; then
  args+=(--headless)
fi

if [[ "${SUB2API_INSECURE:-0}" == "1" ]]; then
  args+=(--sub2api-insecure)
fi

if [[ "${LOOP:-1}" == "1" ]]; then
  args+=(--loop)
fi

exec "$XVFB_RUN_BIN" -a "$PYTHON_BIN" "${args[@]}"
