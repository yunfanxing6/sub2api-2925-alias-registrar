#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/root/sub2api-2925-alias-registrar}"
PYTHON_BIN="${PYTHON_BIN:-$REPO_DIR/.venv/bin/python}"
XVFB_RUN_BIN="${XVFB_RUN_BIN:-/usr/bin/xvfb-run}"

args=(
  "$REPO_DIR/sub2api_browser_domain_registrar.py"
  --sub2api-url "${SUB2API_URL:?SUB2API_URL is required}"
  --admin-api-key "${SUB2API_ADMIN_API_KEY:?SUB2API_ADMIN_API_KEY is required}"
  --group-ids "${GROUP_IDS:-all}"
  --concurrency "${CONCURRENCY:-10}"
  --priority "${PRIORITY:-1}"
  --count "${COUNT:-1}"
  --max-attempts "${MAX_ATTEMPTS:-1}"
  --retry-sleep "${RETRY_SLEEP:-1}"
  --sleep "${SLEEP_SECONDS:-3600}"
  --phone-risk-threshold "${PHONE_RISK_THRESHOLD:-3}"
  --phone-risk-cooldown "${PHONE_RISK_COOLDOWN:-3600}"
  --history-file "${HISTORY_FILE:-$REPO_DIR/domain_history_service.jsonl}"
  --artifacts-dir "${ARTIFACTS_DIR:-$REPO_DIR/browser_artifacts_domain}"
  --mail-domain "${MAIL_DOMAIN:-xingyunfan.dpdns.org}"
  --mail-domains "${MAIL_DOMAINS:-}"
  --mail-local-prefix "${MAIL_LOCAL_PREFIX:-oc}"
  --start-index "${START_INDEX:-1}"
  --state-file "${STATE_FILE:-$REPO_DIR/domain_alias_state.json}"
  --alias-history-file "${ALIAS_HISTORY_FILE:-$REPO_DIR/domain_alias_history.jsonl}"
  --domain-failure-cooldown "${DOMAIN_FAILURE_COOLDOWN:-120}"
  --imap-host "${IMAP_HOST:-imap.2925.com}"
  --imap-port "${IMAP_PORT:-993}"
  --imap-user "${IMAP_USER:?IMAP_USER is required}"
  --imap-password "${IMAP_PASSWORD:?IMAP_PASSWORD is required}"
  --imap-folder "${IMAP_FOLDER:-INBOX}"
  --otp-timeout "${OTP_TIMEOUT:-180}"
  --otp-poll "${OTP_POLL:-3}"
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
