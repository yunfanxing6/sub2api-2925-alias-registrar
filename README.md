# sub2api-2925-alias-registrar

Standalone registrar scripts based on `a.py`, using either temp mail, custom domains, or DuckDuckGo private `@duck.com` aliases, all bridged into Sub2API OAuth APIs.

## What it does

- Uses alias mailbox sequence: `yunfanxing6500@2925.com`, `yunfanxing6501@2925.com`, ...
- Persists next alias index in `2925_alias_state.json` to avoid duplicate registration.
- Uses Sub2API OAuth flow:
  - `POST /api/v1/admin/openai/generate-auth-url`
  - OpenAI registration/login flow (including reauthorize path)
  - `POST /api/v1/admin/openai/create-from-oauth`

## Files

- `sub2api_2925_alias_registrar.py`: 2925 alias mailbox version
- `sub2api_tempmail_registrar.py`: temp mail version based on `registrar_core.py`
- `sub2api_browser_tempmail_registrar.py`: Chromium + Playwright temp mail version
- `sub2api_browser_domain_registrar.py`: Chromium + custom-domain mailbox version
- `sub2api_browser_duck_registrar.py`: Chromium + Duck private-address mailbox version
- `sub2api_account_health_monitor.py`: single-account health monitor and invalid-token repair loop
- `sub2api_domain_history_stats.py`: success-rate summary for domain history JSONL
- `registrar_core.py`: core OpenAI flow adapted from local `a.py`

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`

## Quick start

```bash
python3 sub2api_2925_alias_registrar.py \
  --sub2api-url "https://openaiapi.icu" \
  --admin-api-key "YOUR_ADMIN_API_KEY" \
  --proxy "http://127.0.0.1:7890" \
  --mail-login-email "yunfanxing6@2925.com" \
  --mail-login-password "YOUR_2925_PASSWORD" \
  --count 1
```

Temp mail version:

```bash
python3 sub2api_tempmail_registrar.py \
  --sub2api-url "https://openaiapi.icu" \
  --admin-api-key "YOUR_ADMIN_API_KEY" \
  --proxy "http://127.0.0.1:7890" \
  --mail-sources "tempmail_lol,mailtm" \
  --max-attempts 5 \
  --count 1
```

Chromium browser automation version:

```bash
xvfb-run -a python3 sub2api_browser_tempmail_registrar.py \
  --sub2api-url "https://openaiapi.icu" \
  --admin-api-key "YOUR_ADMIN_API_KEY" \
  --proxy "http://127.0.0.1:7890" \
  --mail-sources "tempmail_lol,mailtm,onesecmail" \
  --group-ids "all" \
  --max-attempts 1 \
  --loop \
  --sleep 0
```

Custom-domain mailbox version:

```bash
xvfb-run -a python3 sub2api_browser_domain_registrar.py \
  --sub2api-url "http://127.0.0.1:8080" \
  --admin-api-key "YOUR_ADMIN_API_KEY" \
  --mail-domains "xingyunfan.dpdns.org,yunfanxing.dpdns.org,openaiapi.icu" \
  --group-ids "all" \
  --imap-host "imap.2925.com" \
  --imap-port 993 \
  --imap-user "yunfanxing6@2925.com" \
  --imap-password "YOUR_IMAP_PASSWORD" \
  --count 1 \
  --max-attempts 1 \
  --domain-failure-cooldown 120
```

Duck private-address version:

```bash
xvfb-run -a python3 sub2api_browser_duck_registrar.py \
  --sub2api-url "http://127.0.0.1:8080" \
  --admin-api-key "YOUR_ADMIN_API_KEY" \
  --duck-token-file "/root/sub2api-2925-alias-registrar/duck_token.txt" \
  --imap-host "imap.2925.com" \
  --imap-port 993 \
  --imap-user "yunfanxing6@2925.com" \
  --imap-password "YOUR_IMAP_PASSWORD" \
  --group-ids "all" \
  --count 1 \
  --max-attempts 1
```

Health monitor:

```bash
xvfb-run -a python3 sub2api_account_health_monitor.py \
  --sub2api-url "http://127.0.0.1:8080" \
  --admin-api-key "YOUR_ADMIN_API_KEY" \
  --imap-host "imap.2925.com" \
  --imap-port 993 \
  --imap-user "yunfanxing6@2925.com" \
  --imap-password "YOUR_IMAP_PASSWORD" \
  --loop \
  --sleep 2 \
  --min-test-interval 600
```

Loop one registration every hour:

```bash
xvfb-run -a python3 sub2api_browser_domain_registrar.py \
  --sub2api-url "http://127.0.0.1:8080" \
  --admin-api-key "YOUR_ADMIN_API_KEY" \
  --mail-domains "xingyunfan.dpdns.org,yunfanxing.dpdns.org,openaiapi.icu" \
  --group-ids "all" \
  --imap-host "imap.2925.com" \
  --imap-port 993 \
  --imap-user "yunfanxing6@2925.com" \
  --imap-password "YOUR_IMAP_PASSWORD" \
  --loop \
  --sleep 3600 \
  --phone-risk-cooldown 3600 \
  --max-attempts 1 \
  --domain-failure-cooldown 120
```

History stats:

```bash
python3 sub2api_domain_history_stats.py domain_history_service.jsonl
```

Systemd templates:

- `deploy/sub2api-browser-domain-registrar.service`: compatibility tempmail service
- `deploy/sub2api-browser-domain-registrar.env.example`: compatibility tempmail env example
- `deploy/sub2api-browser-domain-registrar.logrotate`: compatibility tempmail logrotate
- `deploy/sub2api-browser-tempmail-registrar@.service`: multi-instance tempmail service template
- `deploy/sub2api-browser-tempmail-registrar.env.example`: shared tempmail env example
- `deploy/sub2api-browser-tempmail-registrar.instance.env.example`: per-instance tempmail env example
- `deploy/sub2api-browser-tempmail-registrar.logrotate`: tempmail instance logrotate
- `deploy/sub2api-browser-domain-hourly-registrar.service`: custom-domain service
- `deploy/sub2api-browser-domain-hourly-registrar.env.example`: custom-domain env example
- `deploy/sub2api-browser-domain-hourly-registrar.logrotate`: custom-domain logrotate
- `deploy/sub2api-account-health-monitor.service`: account-health service
- `deploy/sub2api-account-health-monitor.env.example`: account-health env example
- `deploy/sub2api-account-health-monitor.logrotate`: account-health logrotate
- `deploy/sub2api-browser-duck-registrar.service`: Duck registrar service
- `deploy/sub2api-browser-duck-registrar.env.example`: Duck registrar env example
- `deploy/sub2api-browser-duck-registrar.logrotate`: Duck registrar logrotate

## Deployment

Copy the needed env file into `/etc/default`, install the service/logrotate file, then reload systemd.

Duck registrar example:

```bash
install -m 0644 deploy/sub2api-browser-duck-registrar.service /etc/systemd/system/sub2api-browser-duck-registrar.service
install -m 0644 deploy/sub2api-browser-duck-registrar.logrotate /etc/logrotate.d/sub2api-browser-duck-registrar
cp deploy/sub2api-browser-duck-registrar.env.example /etc/default/sub2api-browser-duck-registrar
systemctl daemon-reload
systemctl enable --now sub2api-browser-duck-registrar.service
```

Health monitor example:

```bash
install -m 0644 deploy/sub2api-account-health-monitor.service /etc/systemd/system/sub2api-account-health-monitor.service
install -m 0644 deploy/sub2api-account-health-monitor.logrotate /etc/logrotate.d/sub2api-account-health-monitor
cp deploy/sub2api-account-health-monitor.env.example /etc/default/sub2api-account-health-monitor
systemctl daemon-reload
systemctl enable --now sub2api-account-health-monitor.service
```

Duck production notes:

- On VPS, prefer `DUCK_TOKEN_FILE=/root/sub2api-2925-alias-registrar/duck_token.txt` instead of relying on a logged-in local Chrome profile.
- `managed_account_registry.jsonl` contains sensitive account metadata and stored passwords for managed domain/Duck reauthorization. Treat it like a secret file.
- The Duck script uses the same 2925 IMAP inbox for OTP delivery; only the signup mailbox changes.
- `HEADLESS=0` is the recommended production mode for OpenAI registration flows.
- `HEADLESS=1` has extra spoofing enabled and can still help with some challenge pages, but it is less stable than headful mode on real VPS traffic.

Notes:

- The compatibility tempmail service loops continuously with `SLEEP_SECONDS=0`.
- The multi-instance tempmail service can run `@1`, `@2`, `@3` in parallel with per-instance history/artifact files.
- The custom-domain service loops with `SLEEP_SECONDS=90`.
- The Duck service is intended as a single instance; a modest `SLEEP_SECONDS` is safer than `0` for long-running production use.
- The health monitor tests one account at a time, resumes from the last cursor, and skips accounts tested within the configured minimum interval.
- Failed domains are cooled down for `120` seconds before reuse.
- The production setup can rotate across multiple domains via `--mail-domains`.
- Telegram notifications are supported via `TELEGRAM_BOT_TOKEN` and optional `TELEGRAM_CHAT_ID` in the env file. If `TELEGRAM_CHAT_ID` is empty, the script tries to auto-discover it from `getUpdates` after you send a message to the bot.

## VPS one-liner

```bash
rm -rf ~/sub2api-2925-alias-registrar && \
git clone https://github.com/yunfanxing6/sub2api-2925-alias-registrar.git ~/sub2api-2925-alias-registrar && \
cd ~/sub2api-2925-alias-registrar && \
python3 -m pip install -r requirements.txt && \
python3 sub2api_2925_alias_registrar.py \
  --sub2api-url "https://openaiapi.icu" \
  --admin-api-key "YOUR_ADMIN_API_KEY" \
  --proxy "http://127.0.0.1:7890" \
  --mail-login-email "yunfanxing6@2925.com" \
  --mail-login-password "YOUR_2925_PASSWORD" \
  --count 1
```

## Notes

- If OpenAI enforces phone verification and cannot be bypassed, that attempt may still fail.
- If you change browser spoofing behavior, keep headful and headless paths separate. Headful mode is more successful when it stays close to the browser's natural fingerprint.
- For automation, prefer `--admin-api-key` over email/password login.
- Do not commit real passwords/API keys into this repository.
