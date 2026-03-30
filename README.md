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
