# sub2api-2925-alias-registrar

Standalone registrar scripts based on `a.py`, using either 2925 IMAP mailbox aliases or built-in temp mail, both bridged into Sub2API OAuth APIs.

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

Notes:

- The compatibility tempmail service loops continuously with `SLEEP_SECONDS=0`.
- The multi-instance tempmail service can run `@1`, `@2`, `@3` in parallel with per-instance history/artifact files.
- The custom-domain service loops with `SLEEP_SECONDS=90`.
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
- For automation, prefer `--admin-api-key` over email/password login.
- Do not commit real passwords/API keys into this repository.
