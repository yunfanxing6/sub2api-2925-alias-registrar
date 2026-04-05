#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$SCRIPT_DIR}"
PYTHON_BIN="${PYTHON_BIN:-$REPO_DIR/.venv/bin/python}"
XVFB_RUN_BIN="${XVFB_RUN_BIN:-/usr/bin/xvfb-run}"

args=(
  "$REPO_DIR/sub2api_account_health_monitor.py"
  --sub2api-url "${SUB2API_URL:?SUB2API_URL is required}"
  --admin-api-key "${SUB2API_ADMIN_API_KEY:?SUB2API_ADMIN_API_KEY is required}"
  --group-ids "${GROUP_IDS:-all}"
  --concurrency "${CONCURRENCY:-10}"
  --priority "${PRIORITY:-1}"
  --sleep "${SLEEP_SECONDS:-2}"
  --min-test-interval "${MIN_TEST_INTERVAL_SECONDS:-600}"
  --history-file "${HISTORY_FILE:-$REPO_DIR/account_health_history.jsonl}"
  --state-file "${STATE_FILE:-$REPO_DIR/account_health_state.json}"
  --artifacts-dir "${ARTIFACTS_DIR:-$REPO_DIR/browser_artifacts_account_health}"
  --mail-domain "${MAIL_DOMAIN:-xingyunfan.dpdns.org}"
  --mail-domains "${MAIL_DOMAINS:-}"
  --imap-host "${IMAP_HOST:-imap.2925.com}"
  --imap-port "${IMAP_PORT:-993}"
  --imap-user "${IMAP_USER:?IMAP_USER is required}"
  --imap-password "${IMAP_PASSWORD:?IMAP_PASSWORD is required}"
  --imap-folder "${IMAP_FOLDER:-INBOX}"
  --managed-accounts-file "${MANAGED_ACCOUNTS_FILE:-$REPO_DIR/managed_account_registry.jsonl}"
  --page-size "${PAGE_SIZE:-100}"
  --max-pages "${MAX_PAGES:-100}"
  --reauthorize-timeout "${REAUTHORIZE_TIMEOUT:-600}"
  --telegram-chat-cache-file "${TELEGRAM_CHAT_CACHE_FILE:-$REPO_DIR/telegram_chat_id.txt}"
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

if [[ -n "${TEST_MODEL_ID:-}" ]]; then
  args+=(--test-model-id "$TEST_MODEL_ID")
fi

if [[ -n "${TEST_PROMPT:-}" ]]; then
  args+=(--test-prompt "$TEST_PROMPT")
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
