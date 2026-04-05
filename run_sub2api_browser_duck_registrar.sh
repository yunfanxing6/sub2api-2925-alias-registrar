#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$SCRIPT_DIR}"
PYTHON_BIN="${PYTHON_BIN:-$REPO_DIR/.venv/bin/python}"
XVFB_RUN_BIN="${XVFB_RUN_BIN:-/usr/bin/xvfb-run}"
DEFAULT_HOME="${HOME:-/root}"

args=(
  "$REPO_DIR/sub2api_browser_duck_registrar.py"
  --sub2api-url "${SUB2API_URL:?SUB2API_URL is required}"
  --admin-api-key "${SUB2API_ADMIN_API_KEY:?SUB2API_ADMIN_API_KEY is required}"
  --group-ids "${GROUP_IDS:-all}"
  --concurrency "${CONCURRENCY:-10}"
  --priority "${PRIORITY:-1}"
  --count "${COUNT:-1}"
  --max-attempts "${MAX_ATTEMPTS:-1}"
  --attempt-timeout "${ATTEMPT_TIMEOUT:-600}"
  --retry-sleep "${RETRY_SLEEP:-1}"
  --sleep "${SLEEP_SECONDS:-0}"
  --history-file "${HISTORY_FILE:-$REPO_DIR/duck_history_service.jsonl}"
  --artifacts-dir "${ARTIFACTS_DIR:-$REPO_DIR/browser_artifacts}"
  --managed-accounts-file "${MANAGED_ACCOUNTS_FILE:-$REPO_DIR/managed_account_registry.jsonl}"
  --imap-host "${IMAP_HOST:-imap.2925.com}"
  --imap-port "${IMAP_PORT:-993}"
  --imap-user "${IMAP_USER:-yunfanxing6@2925.com}"
  --imap-folder "${IMAP_FOLDER:-INBOX}"
  --otp-timeout "${OTP_TIMEOUT:-180}"
  --otp-poll "${OTP_POLL:-3}"
  --duck-profile-dir "${DUCK_PROFILE_DIR:-$DEFAULT_HOME/.config/google-chrome/Default}"
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

args+=(--telegram-chat-cache-file "${TELEGRAM_CHAT_CACHE_FILE:-$REPO_DIR/telegram_chat_id.txt}")

if [[ -n "${DUCK_TOKEN:-}" ]]; then
  args+=(--duck-token "$DUCK_TOKEN")
fi

if [[ -n "${DUCK_TOKEN_FILE:-}" ]]; then
  args+=(--duck-token-file "$DUCK_TOKEN_FILE")
fi

if [[ -n "${DUCK_EXTENSION_ID:-}" ]]; then
  args+=(--duck-extension-id "$DUCK_EXTENSION_ID")
fi

if [[ -n "${DUCK_ALIAS_API_URL:-}" ]]; then
  args+=(--duck-alias-api-url "$DUCK_ALIAS_API_URL")
fi

if [[ -n "${DUCK_REQUEST_TIMEOUT:-}" ]]; then
  args+=(--duck-request-timeout "$DUCK_REQUEST_TIMEOUT")
fi

if [[ -n "${IMAP_PASSWORD:-}" ]]; then
  args+=(--imap-password "$IMAP_PASSWORD")
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

if [[ "${IMAP_INSECURE:-0}" == "1" ]]; then
  args+=(--imap-insecure)
fi

if [[ "${LOOP:-1}" == "1" ]]; then
  args+=(--loop)
fi

exec "$XVFB_RUN_BIN" -a "$PYTHON_BIN" "${args[@]}"
