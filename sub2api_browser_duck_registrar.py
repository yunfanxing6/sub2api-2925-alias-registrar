#!/usr/bin/env python3
import argparse
import getpass
import json
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from managed_account_store import ManagedAccountStore
from playwright.sync_api import sync_playwright

import registrar_core as registrar
import sub2api_browser_tempmail_registrar as browser_flow
from sub2api_browser_domain_registrar import RoutingIMAPClient
from sub2api_browser_tempmail_registrar import FlowError, NeedReauth, SkipMailbox, TelegramNotifier, append_history, attempt_deadline, post_configure_account
from sub2api_tempmail_registrar import Sub2APIClient, normalize_base_url


DUCK_ALIAS_API_URL = "https://quack.duckduckgo.com/api/email/addresses"
DUCK_EXTENSION_ID = "bkdgflcldnnnapblkhphbgpggdiikppg"


def normalize_duck_alias(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    if "@" not in value:
        value = f"{value}@duck.com"
    if not value.endswith("@duck.com"):
        return ""
    return value


class DuckTokenProvider:
    def __init__(
        self,
        *,
        token: str,
        token_file: str,
        profile_dir: str,
        extension_id: str,
        alias_api_url: str,
        request_timeout: int,
    ) -> None:
        self.static_token = str(token or "").strip()
        self.token_file = str(token_file or "").strip()
        self.profile_dir = str(profile_dir or "").strip()
        self.extension_id = str(extension_id or DUCK_EXTENSION_ID).strip() or DUCK_EXTENSION_ID
        self.alias_api_url = str(alias_api_url or DUCK_ALIAS_API_URL).strip() or DUCK_ALIAS_API_URL
        self.request_timeout = max(5, int(request_timeout or 20))

    @staticmethod
    def _extract_pattern(blob: str, pattern: str) -> str:
        match = re.search(pattern, blob)
        if not match:
            return ""
        return str(match.group(1) or "").strip()

    def _extract_profile_state(self) -> dict[str, str]:
        if not self.profile_dir:
            return {}
        settings_dir = Path(self.profile_dir) / "Local Extension Settings" / self.extension_id
        if not settings_dir.is_dir():
            return {}

        token = ""
        username = ""
        next_alias = ""
        candidates = sorted((item for item in settings_dir.iterdir() if item.is_file()), key=lambda item: item.stat().st_mtime, reverse=True)
        for path in candidates:
            try:
                raw = path.read_bytes().decode("latin-1", "ignore")
            except Exception:
                continue
            if not token:
                token = self._extract_pattern(raw, r'"token":"([a-z0-9]{20,})')
            if not username:
                username = self._extract_pattern(raw, r'"userName":"([a-z0-9_]+)')
            if not next_alias:
                next_alias = normalize_duck_alias(self._extract_pattern(raw, r'"nextAlias":"([a-z0-9-]+)'))
            if token and username:
                break
        return {
            "token": token,
            "username": username,
            "next_alias": next_alias,
        }

    def resolve_token(self) -> tuple[str, dict[str, str]]:
        if self.static_token:
            return self.static_token, {"source": "arg"}
        if self.token_file:
            token = Path(self.token_file).read_text(encoding="utf-8").strip()
            if token:
                return token, {"source": "file", "token_file": self.token_file}

        profile_state = self._extract_profile_state()
        token = str(profile_state.get("token") or "").strip()
        if token:
            profile_state["source"] = "profile"
            profile_state["profile_dir"] = self.profile_dir
            return token, profile_state
        raise FlowError("duck token not found; provide --duck-token/--duck-token-file or a logged-in Chrome profile")

    def generate_alias(self, proxies: Any = None) -> tuple[str, dict[str, str]]:
        token, metadata = self.resolve_token()
        req = urllib.request.Request(
            self.alias_api_url,
            data=b"",
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        try:
            opener = urllib.request.build_opener()
            proxy_url = str(proxies or "").strip()
            if proxy_url:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
            with opener.open(req, timeout=self.request_timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace").strip()
            raise FlowError(f"duck alias request failed: HTTP {exc.code} {body[:200]}") from exc
        except Exception as exc:
            raise FlowError(f"duck alias request failed: {exc}") from exc

        alias = normalize_duck_alias(str(payload.get("address") or payload.get("alias") or ""))
        if not alias:
            raise FlowError(f"duck alias response missing address: {payload}")
        metadata = dict(metadata)
        metadata["alias"] = alias
        return alias, metadata


def install_duck_mail_bridge(*, duck_provider: DuckTokenProvider, imap_client: RoutingIMAPClient, otp_timeout: int, otp_poll_interval: float) -> None:
    seen_by_token: dict[str, set[int]] = {}

    def parse_token(token: str) -> tuple[str, int]:
        raw = (token or "").strip()
        parts = raw.split(":", 2)
        if len(parts) == 3 and parts[0] == "duck":
            try:
                return raw, int(parts[1])
            except Exception:
                return raw, 0
        return raw, 0

    def patched_get_email_and_token(proxies: Any = None) -> tuple[str, str]:
        email_addr, metadata = duck_provider.generate_alias(proxies=proxies)
        baseline_uid = imap_client.latest_uid()
        token = f"duck:{baseline_uid}:{email_addr}"
        username = str(metadata.get("username") or "").strip()
        source = str(metadata.get("source") or "duck")
        label = f"source={source}"
        if username:
            label += f", user={username}@duck.com"
        print(f"[*] duck mailbox: {email_addr} ({label}, baseline_uid={baseline_uid})")
        return email_addr, token

    def patched_get_oai_code(token: str, email_addr: str, proxies: Any = None, seen_msg_ids: set | None = None) -> str:
        del proxies
        tok_key, baseline_uid = parse_token(token)
        seen = seen_by_token.setdefault(tok_key, set())
        print(f"[*] waiting otp for {email_addr}", end="", flush=True)
        code, uid = imap_client.wait_otp_code(
            target_email=email_addr,
            since_uid=baseline_uid,
            seen_uids=seen,
            timeout_sec=otp_timeout,
            poll_interval_sec=otp_poll_interval,
        )
        if code and uid:
            seen.add(uid)
            if seen_msg_ids is not None:
                try:
                    seen_msg_ids.add(str(uid))
                except Exception:
                    pass
            print(f" got otp: {code}")
            return code
        print(" timeout, no otp received")
        return ""

    registrar.get_email_and_token = patched_get_email_and_token
    registrar.get_oai_code = patched_get_oai_code
    registrar.get_oai_verify = patched_get_oai_code


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Browser-driven DuckDuckGo private-address registrar using Duck alias API + IMAP + Sub2API OAuth bridge."
    )
    parser.add_argument("--proxy", default=None, help="Registration proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--sub2api-url", required=True, help="Example: https://openaiapi.icu")
    parser.add_argument("--sub2api-insecure", action="store_true", help="Skip sub2api TLS certificate validation")
    parser.add_argument("--sub2api-timeout", type=int, default=25, help="Sub2API HTTP timeout in seconds")
    parser.add_argument("--admin-api-key", default="", help="Admin x-api-key (recommended)")
    parser.add_argument("--admin-token", default="", help="Admin JWT bearer token")
    parser.add_argument("--admin-email", default="", help="Admin login email")
    parser.add_argument("--admin-password", default="", help="Admin login password")
    parser.add_argument("--login-turnstile-token", default="", help="Optional turnstile token for /auth/login")
    parser.add_argument("--sub2api-proxy-id", type=int, default=None, help="Sub2API proxy_id to bind")
    parser.add_argument("--redirect-uri", default="http://localhost:1455/auth/callback", help="OAuth redirect_uri")
    parser.add_argument("--group-ids", default="all", help="Account group IDs, comma-separated, or 'all'")
    parser.add_argument("--concurrency", type=int, default=10, help="Account concurrency")
    parser.add_argument("--priority", type=int, default=1, help="Account priority")
    parser.add_argument("--count", type=int, default=1, help="How many accounts to register this run")
    parser.add_argument("--max-attempts", type=int, default=3, help="Max mailbox attempts per target account; use 0 for unlimited retries until success")
    parser.add_argument("--attempt-timeout", type=float, default=600.0, help="Hard timeout seconds for a single registration attempt")
    parser.add_argument("--retry-sleep", type=float, default=2.0, help="Sleep seconds between failed attempts")
    parser.add_argument("--sleep", type=float, default=0.0, help="Sleep seconds between accounts")
    parser.add_argument("--loop", action="store_true", help="Run continuously instead of stopping after count accounts")
    parser.add_argument("--history-file", default="", help="Optional JSONL run history file")
    parser.add_argument("--chromium-path", default="", help="Optional Chromium executable path")
    parser.add_argument("--headless", action="store_true", help="Launch Chromium in headless mode")
    parser.add_argument("--artifacts-dir", default="artifacts", help="Directory for browser profiles and screenshots")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--telegram-bot-token", default="", help="Optional Telegram bot token for notifications")
    parser.add_argument("--telegram-chat-id", default="", help="Optional Telegram chat id; auto-detected from getUpdates if empty")
    parser.add_argument("--telegram-chat-cache-file", default="telegram_chat_id.txt", help="File used to persist resolved Telegram chat id")
    parser.add_argument("--imap-host", default="imap.2925.com", help="IMAP host")
    parser.add_argument("--imap-port", type=int, default=993, help="IMAP port")
    parser.add_argument("--imap-user", default="yunfanxing6@2925.com", help="IMAP username")
    parser.add_argument("--imap-password", default="", help="IMAP password")
    parser.add_argument("--imap-folder", default="INBOX", help="IMAP mailbox folder")
    parser.add_argument("--imap-insecure", action="store_true", help="Skip IMAP TLS certificate validation")
    parser.add_argument("--otp-timeout", type=int, default=180, help="OTP wait timeout seconds")
    parser.add_argument("--otp-poll", type=float, default=3.0, help="OTP polling interval seconds")
    parser.add_argument("--managed-accounts-file", default="managed_account_registry.jsonl", help="JSONL file used to track managed account metadata")
    parser.add_argument("--duck-token", default="", help="Duck alias bearer token")
    parser.add_argument("--duck-token-file", default="", help="File containing Duck alias bearer token")
    parser.add_argument("--duck-profile-dir", default=str(Path.home() / ".config/google-chrome/Default"), help="Chrome profile dir used to read Duck extension userData")
    parser.add_argument("--duck-extension-id", default=DUCK_EXTENSION_ID, help="DuckDuckGo extension id")
    parser.add_argument("--duck-alias-api-url", default=DUCK_ALIAS_API_URL, help="Duck alias API endpoint")
    parser.add_argument("--duck-request-timeout", type=int, default=20, help="Duck alias API timeout seconds")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    browser_flow.DEBUG_LOGS = bool(args.debug)

    try:
        sub2api_url = normalize_base_url(args.sub2api_url)
        if not args.admin_api_key and not args.admin_token and not args.admin_email:
            raise ValueError("provide --admin-api-key or --admin-token or --admin-email")
        if args.count <= 0:
            raise ValueError("count must be > 0")
        if args.max_attempts < 0:
            raise ValueError("max-attempts must be >= 0")
        if args.otp_timeout <= 0 or args.otp_poll <= 0:
            raise ValueError("otp timeout/poll must be > 0")
    except Exception as exc:
        print(f"[config error] {exc}")
        return 2

    admin_password = args.admin_password
    if not args.admin_api_key and not args.admin_token and args.admin_email and not admin_password:
        admin_password = getpass.getpass("Sub2API admin password: ")
    imap_password = args.imap_password or getpass.getpass("IMAP password: ")

    artifacts_dir = Path(args.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    duck_provider = DuckTokenProvider(
        token=args.duck_token,
        token_file=args.duck_token_file,
        profile_dir=args.duck_profile_dir,
        extension_id=args.duck_extension_id,
        alias_api_url=args.duck_alias_api_url,
        request_timeout=args.duck_request_timeout,
    )
    try:
        _, token_meta = duck_provider.resolve_token()
    except Exception as exc:
        print(f"[config error] {exc}")
        return 2

    imap_client = RoutingIMAPClient(
        host=args.imap_host,
        port=args.imap_port,
        username=args.imap_user,
        password=imap_password,
        folder=args.imap_folder,
        insecure=args.imap_insecure,
    )
    install_duck_mail_bridge(
        duck_provider=duck_provider,
        imap_client=imap_client,
        otp_timeout=args.otp_timeout,
        otp_poll_interval=args.otp_poll,
    )

    client = Sub2APIClient(
        base_url=sub2api_url,
        timeout=args.sub2api_timeout,
        insecure=args.sub2api_insecure,
        admin_api_key=args.admin_api_key,
        admin_token=args.admin_token,
        admin_email=args.admin_email,
        admin_password=admin_password,
        login_turnstile_token=args.login_turnstile_token,
    )
    notifier = TelegramNotifier(args.telegram_bot_token, args.telegram_chat_id, args.telegram_chat_cache_file)
    managed_accounts = ManagedAccountStore(args.managed_accounts_file)

    print("[Info] browser duck-mail + Sub2API registrar started")
    print(f"[Info] duck token source: {token_meta.get('source', 'unknown')}")
    print(f"[Info] imap inbox: {args.imap_user}")

    success = 0
    failed_accounts = 0
    total_attempts = 0
    skipped_mailboxes = 0

    def print_stats() -> None:
        processed = success + failed_accounts
        success_rate = (success / processed * 100.0) if processed else 0.0
        attempt_success_rate = (success / total_attempts * 100.0) if total_attempts else 0.0
        print(
            "[stats] accounts=%s success=%s failed=%s success_rate=%.1f%% attempts=%s attempt_success_rate=%.1f%% skipped=%s"
            % (processed, success, failed_accounts, success_rate, total_attempts, attempt_success_rate, skipped_mailboxes)
        )

    max_attempts_label = "unlimited" if args.max_attempts == 0 else str(args.max_attempts)

    with sync_playwright() as pw:
        idx = 0
        while True:
            idx += 1
            if not args.loop and idx > args.count:
                break
            print(f"\n========== account {idx}/{args.count} ==========")
            account_done = False
            account_last_reason = ""
            attempt = 0
            while True:
                attempt += 1
                if args.max_attempts > 0 and attempt > args.max_attempts:
                    break
                total_attempts += 1
                print(f"[*] attempt {attempt}/{max_attempts_label}")
                started = time.time()
                email_addr = ""
                password = ""
                browser = None
                context = None
                try:
                    with attempt_deadline(args.attempt_timeout):
                        email_addr, dev_token = registrar.get_email_and_token(args.proxy)
                        if not email_addr or not dev_token:
                            raise FlowError("duck mail acquisition failed")
                        password = secrets.token_urlsafe(18)

                        auth_url_1, session_id_1 = client.generate_auth_url(redirect_uri=args.redirect_uri, proxy_id=args.sub2api_proxy_id)
                        print("[*] sub2api generate-auth-url #1 (register)")

                        browser, context = browser_flow.launch_context(
                            pw,
                            executable_path=args.chromium_path,
                            headless=args.headless,
                            artifacts_dir=str(artifacts_dir),
                        )
                        page = context.pages[0] if context.pages else context.new_page()

                        try:
                            callback_url = browser_flow.perform_auth_flow(
                                page=page,
                                auth_url=auth_url_1,
                                email=email_addr,
                                password=password,
                                dev_token=dev_token,
                                proxies=args.proxy,
                                redirect_uri=args.redirect_uri,
                                signup=True,
                            )
                            session_id_final = session_id_1
                        except NeedReauth:
                            print("[*] phone verification detected, restarting with login auth url")
                            auth_url_2, session_id_2 = client.generate_auth_url(redirect_uri=args.redirect_uri, proxy_id=args.sub2api_proxy_id)
                            print("[*] sub2api generate-auth-url #2 (login-reauthorize)")
                            callback_url = browser_flow.perform_auth_flow(
                                page=page,
                                auth_url=auth_url_2,
                                email=email_addr,
                                password=password,
                                dev_token=dev_token,
                                proxies=args.proxy,
                                redirect_uri=args.redirect_uri,
                                signup=False,
                            )
                            session_id_final = session_id_2

                        parsed = registrar._parse_callback_url(callback_url)
                        created = client.create_from_oauth(
                            session_id=session_id_final,
                            code=parsed.get("code", ""),
                            state=parsed.get("state", ""),
                            redirect_uri=args.redirect_uri,
                            proxy_id=args.sub2api_proxy_id,
                            name=email_addr,
                            group_ids=[],
                            concurrency=args.concurrency,
                            priority=args.priority,
                        )
                        account_id = int(created.get("id") or 0)
                        if account_id > 0:
                            created = post_configure_account(client, account_id=account_id, platform="openai", group_ids_raw=args.group_ids)
                    print(f"[OK] account created: id={created.get('id')}, name={created.get('name')}")
                    managed_accounts.record_duck_success(email_addr=email_addr, password=password, account_id=int(created.get("id") or 0))
                    append_history(
                        args.history_file,
                        {
                            "kind": "attempt",
                            "at": time.time(),
                            "success": True,
                            "email": email_addr,
                            "email_domain": "duck.com",
                            "attempt": attempt,
                            "account": created,
                            "elapsed_sec": round(time.time() - started, 2),
                        },
                    )
                    success += 1
                    account_done = True
                    try:
                        notifier.send(f"sub2api success\naccount={created.get('name')}\nemail={email_addr}\nid={created.get('id')}\nsuccess={success}")
                    except Exception:
                        pass
                    append_history(
                        args.history_file,
                        {
                            "kind": "account_result",
                            "at": time.time(),
                            "success": True,
                            "account_index": idx,
                            "email": email_addr,
                            "attempts_used": attempt,
                            "account": created,
                        },
                    )
                    print_stats()
                    break
                except Exception as exc:
                    account_last_reason = str(exc)
                    if context is not None:
                        try:
                            page = context.pages[0] if context.pages else None
                            if page is not None:
                                browser_flow.debug_log(f"[debug] page_url={page.url}")
                                browser_flow.debug_log(f"[debug] page_title={page.title()}")
                                browser_flow.debug_log(f"[debug] page_text={browser_flow.visible_text(page)[:800]}")
                        except Exception:
                            pass
                    reason = str(exc)
                    if isinstance(exc, SkipMailbox):
                        skipped_mailboxes += 1
                        print(f"[skip] attempt {attempt}: {reason}; switching to next duck mailbox")
                    else:
                        print(f"[failed] attempt {attempt}: {reason}")
                    append_history(
                        args.history_file,
                        {
                            "kind": "attempt",
                            "at": time.time(),
                            "success": False,
                            "email": email_addr,
                            "attempt": attempt,
                            "error": reason,
                            "skip_mailbox": isinstance(exc, SkipMailbox),
                            "elapsed_sec": round(time.time() - started, 2),
                        },
                    )
                    should_retry = args.max_attempts == 0 or attempt < args.max_attempts
                    if should_retry and args.retry_sleep > 0:
                        time.sleep(args.retry_sleep)
                finally:
                    try:
                        if context is not None:
                            context.close()
                    except Exception:
                        pass
                    try:
                        if browser is not None:
                            browser.close()
                    except Exception:
                        pass

            if not account_done:
                print(f"[failed] account {idx} exhausted {max_attempts_label} attempts")
                failed_accounts += 1
                try:
                    notifier.send(f"sub2api failed\naccount_index={idx}\nemail={email_addr or 'unknown'}\nfailed_after_attempts={max_attempts_label}\nreason={account_last_reason or 'unknown'}")
                except Exception:
                    pass
                append_history(
                    args.history_file,
                    {
                        "kind": "account_result",
                        "at": time.time(),
                        "success": False,
                        "account_index": idx,
                        "email": email_addr,
                        "attempts_used": attempt,
                        "reason": account_last_reason,
                    },
                )
                print_stats()
            if (args.loop or idx < args.count) and args.sleep > 0:
                time.sleep(args.sleep)

    total_accounts = success + failed_accounts
    print(f"\nDone: success {success}/{total_accounts or args.count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
