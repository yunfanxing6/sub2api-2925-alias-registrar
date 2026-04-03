#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/root/sub2api-2925-alias-registrar}"

exec "$REPO_DIR/run_sub2api_browser_tempmail_registrar.sh"
