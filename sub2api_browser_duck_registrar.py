#!/usr/bin/env python3
import argparse
import email
import getpass
import json
import re
import secrets
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from managed_account_store import ManagedAccountStore
from playwright.sync_api import BrowserContext, Page, sync_playwright

import registrar_core as registrar
import sub2api_browser_tempmail_registrar as browser_flow
from sub2api_browser_domain_registrar import RoutingIMAPClient
from sub2api_browser_tempmail_registrar import FlowError, NeedReauth, SkipMailbox, TelegramNotifier, append_history, attempt_deadline, post_configure_account
from sub2api_2925_alias_registrar import decode_header_str, extract_message_text
from sub2api_tempmail_registrar import Sub2APIClient, normalize_base_url


DUCK_ALIAS_API_URL = "https://quack.duckduckgo.com/api/email/addresses"
DUCK_EXTENSION_ID = "bkdgflcldnnnapblkhphbgpggdiikppg"
DUCK_LOGIN_LINK_RE = re.compile(r"https://duckduckgo\.com/email/login\?otp=([a-z-]+)&user=([a-z0-9_]+)", re.I)
DUCK_SIGNIN_SUBJECT_TOKEN = "duckduckgo one-time passphrase"


def normalize_duck_alias(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    if "@" not in value:
        value = f"{value}@duck.com"
    if not value.endswith("@duck.com"):
        return ""
    return value


def normalize_duck_username(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if value.endswith("@duck.com"):
        value = value[:-9]
    return value


def resolve_duck_extension_path(raw: str) -> Path:
    path = Path(str(raw or "").strip()).expanduser()
    if not str(path):
        raise FlowError("duck extension path is required")
    if path.is_dir() and (path / "manifest.json").exists():
        return path
    if path.is_dir():
        candidates = sorted([item for item in path.iterdir() if item.is_dir() and (item / "manifest.json").exists()], key=lambda item: item.stat().st_mtime, reverse=True)
        if candidates:
            return candidates[0]
    raise FlowError(f"duck extension path not found: {path}")


def load_blocked_duck_aliases_from_history(history_file: str) -> set[str]:
    path = Path(history_file or "")
    if not history_file or not path.exists():
        return set()
    blocked: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind") or "")
            email_addr = ""
            if kind == "account_result" and bool(row.get("success")):
                email_addr = normalize_duck_alias(str(row.get("email") or ""))
            elif kind == "attempt" and bool(row.get("skip_mailbox")):
                email_addr = normalize_duck_alias(str(row.get("email") or ""))
            if email_addr:
                blocked.add(email_addr)
    except Exception:
        return blocked
    return blocked


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

    def _request_alias(self, proxies: Any = None) -> tuple[str, dict[str, str]]:
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

    def generate_alias(
        self,
        proxies: Any = None,
        *,
        exclude_aliases: set[str] | None = None,
        max_refresh_attempts: int = 3,
        refresh_sleep_sec: float = 1.0,
    ) -> tuple[str, dict[str, str]]:
        blocked = {normalize_duck_alias(item) for item in (exclude_aliases or set()) if normalize_duck_alias(item)}
        attempts = max(1, int(max_refresh_attempts or 1))
        last_alias = ""
        last_metadata: dict[str, str] = {}
        for idx in range(1, attempts + 1):
            alias, metadata = self._request_alias(proxies=proxies)
            last_alias = alias
            last_metadata = metadata
            if alias not in blocked:
                return alias, metadata
            if idx < attempts and refresh_sleep_sec > 0:
                time.sleep(refresh_sleep_sec)
        raise SkipMailbox(f"duck alias did not rotate from blocked alias: {last_alias or 'unknown'}")


class DuckExtensionProvider:
    def __init__(
        self,
        *,
        duck_username: str,
        extension_path: str,
        browser_profile_dir: str,
        recovery_email: str,
        chromium_path: str,
        headless: bool,
        proxy: str,
        login_timeout: int,
        login_poll_interval: float,
        imap_client: RoutingIMAPClient,
    ) -> None:
        self.duck_username = normalize_duck_username(duck_username)
        if not self.duck_username:
            raise FlowError("duck username is required")
        self.extension_path = resolve_duck_extension_path(extension_path)
        self.browser_profile_dir = Path(browser_profile_dir).expanduser()
        self.recovery_email = str(recovery_email or "").strip().lower()
        self.chromium_path = str(chromium_path or "").strip()
        self.headless = bool(headless)
        self.proxy = str(proxy or "").strip()
        self.login_timeout = max(30, int(login_timeout or 120))
        self.login_poll_interval = max(1.0, float(login_poll_interval or 3.0))
        self.imap_client = imap_client
        self.playwright = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def start(self, playwright) -> None:
        self.playwright = playwright
        if self.context is not None:
            return
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(self.browser_profile_dir),
            "headless": self.headless,
            "ignore_https_errors": True,
            "executable_path": self.chromium_path or None,
            "args": [
                f"--disable-extensions-except={self.extension_path}",
                f"--load-extension={self.extension_path}",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--ignore-certificate-errors",
                "--allow-insecure-localhost",
                "--disable-features=CertVerifierService,ChromeRootStoreUsed",
                "--start-maximized",
            ],
        }
        if self.proxy:
            launch_kwargs["proxy"] = {"server": self.proxy}
        self.context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            self.context = None
            self.page = None

    def reset_profile(self) -> None:
        self.close()
        shutil.rmtree(self.browser_profile_dir, ignore_errors=True)
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        if self.playwright is None:
            raise FlowError("duck playwright context not initialized")
        self.start(self.playwright)

    def _ensure_page(self) -> Page:
        if self.page is None:
            raise FlowError("duck browser page not initialized")
        return self.page

    def _goto_autofill(self) -> Page:
        page = self._ensure_page()
        browser_flow.goto_with_retry(page, "https://duckduckgo.com/email/settings/autofill")
        return page

    def _is_signed_in(self, page: Page) -> bool:
        text = browser_flow.visible_text(page)
        if f"{self.duck_username}@duck.com" in text and "Private Duck Address Generator" in text:
            return True
        if self._read_current_alias(page):
            return True
        try:
            button = page.get_by_role("button", name=re.compile(r"Generate Private Duck Address", re.I))
            return button.count() > 0 and button.first.is_visible()
        except Exception:
            return False

    def _find_login_textbox(self, page: Page):
        candidates = [
            page.locator("input:not([readonly])"),
            page.locator("input[type='text']:not([readonly])"),
            page.get_by_role("textbox"),
        ]
        for locator in candidates:
            try:
                count = locator.count()
            except Exception:
                count = 0
            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if not item.is_visible():
                        continue
                    if item.get_attribute("readonly") is not None:
                        continue
                    return item
                except Exception:
                    continue
        return None

    def _read_current_alias(self, page: Page) -> str:
        for locator in [page.locator("input[type='text']"), page.get_by_role("textbox")]:
            try:
                count = locator.count()
            except Exception:
                count = 0
            for idx in range(count):
                try:
                    value = locator.nth(idx).input_value(timeout=2000).strip()
                except Exception:
                    continue
                alias = normalize_duck_alias(value)
                if alias:
                    return alias
        return ""

    def _wait_for_login_link(self, *, since_uid: int) -> str:
        deadline = time.time() + self.login_timeout
        target_route = self.recovery_email
        while time.time() < deadline:
            conn = None
            try:
                conn = self.imap_client._connect()
                latest_seq = self.imap_client._select_count(conn)
                start_seq = max(1, latest_seq - 120)
                for seq in range(latest_seq, start_seq - 1, -1):
                    uid, raw_email = self.imap_client._fetch_uid_rfc822(conn, seq)
                    if uid <= since_uid or not raw_email:
                        continue
                    msg = email.message_from_bytes(raw_email)
                    sender = decode_header_str(msg.get("From", ""))
                    subject = decode_header_str(msg.get("Subject", ""))
                    to_header = decode_header_str(msg.get("To", ""))
                    delivered_to = decode_header_str(msg.get("Delivered-To", ""))
                    orig_to = decode_header_str(msg.get("X-Original-To", ""))
                    body = extract_message_text(msg)
                    route_headers = "\n".join([to_header, delivered_to, orig_to]).lower()
                    if target_route and any([to_header.strip(), delivered_to.strip(), orig_to.strip()]) and target_route not in route_headers:
                        continue
                    haystack = "\n".join([sender, subject, body]).lower()
                    if DUCK_SIGNIN_SUBJECT_TOKEN not in haystack and "support@duck.com" not in haystack:
                        continue
                    match = DUCK_LOGIN_LINK_RE.search(body)
                    if not match:
                        continue
                    if normalize_duck_username(match.group(2)) != self.duck_username:
                        continue
                    return match.group(0)
            finally:
                if conn is not None:
                    try:
                        conn.logout()
                    except Exception:
                        pass
            time.sleep(self.login_poll_interval)
        raise FlowError(f"duck sign-in link not received for {self.duck_username}@duck.com")

    def _ensure_signed_in(self) -> Page:
        page = self._goto_autofill()
        text = browser_flow.visible_text(page)
        if self._is_signed_in(page):
            return page
        if "Sign Out" in text and f"{self.duck_username}@duck.com" not in text:
            if browser_flow.maybe_click(page.get_by_role("button", name=re.compile(r"Sign Out", re.I))):
                time.sleep(2)
                page = self._goto_autofill()
                text = browser_flow.visible_text(page)
        if self._is_signed_in(page):
            return page

        login_link = ""
        for attempt in range(1, 4):
            text = browser_flow.visible_text(page)
            baseline_uid = self.imap_client.latest_uid()
            if "Check your inbox!" in text:
                if not browser_flow.maybe_click(page.get_by_role("button", name=re.compile(r"Resend", re.I))):
                    raise FlowError("duck resend button not found")
            else:
                textbox = self._find_login_textbox(page)
                if textbox is None:
                    if attempt >= 3:
                        raise FlowError("duck login textbox not found")
                    page = self._goto_autofill()
                    continue
                textbox.fill(self.duck_username)
                if not browser_flow.maybe_click(page.get_by_role("button", name=re.compile(r"Continue", re.I))):
                    raise FlowError("duck continue button not found")
            try:
                login_link = self._wait_for_login_link(since_uid=baseline_uid)
                break
            except FlowError:
                if attempt >= 3:
                    raise
                page = self._goto_autofill()
        if not login_link:
            raise FlowError(f"duck sign-in link not received for {self.duck_username}@duck.com")
        browser_flow.goto_with_retry(page, login_link)
        deadline = time.time() + self.login_timeout
        while time.time() < deadline:
            time.sleep(2)
            if self._is_signed_in(page):
                return page
            if "/email/login" in page.url:
                continue
            page = self._goto_autofill()
            if self._is_signed_in(page):
                return page
        raise FlowError(f"duck sign-in did not complete for {self.duck_username}@duck.com")

    def generate_alias(
        self,
        proxies: Any = None,
        *,
        exclude_aliases: set[str] | None = None,
        max_refresh_attempts: int = 3,
        refresh_sleep_sec: float = 1.0,
        allow_profile_reset: bool = True,
    ) -> tuple[str, dict[str, str]]:
        page = self._ensure_signed_in()
        blocked = {normalize_duck_alias(item) for item in (exclude_aliases or set()) if normalize_duck_alias(item)}
        attempts = max(1, int(max_refresh_attempts or 1))
        previous_alias = ""
        for idx in range(1, attempts + 1):
            current_alias = self._read_current_alias(page)
            if current_alias and current_alias not in blocked:
                return current_alias, {"source": "browser_extension", "username": self.duck_username, "alias": current_alias}
            before_click_alias = current_alias or previous_alias
            if not browser_flow.maybe_click(page.get_by_role("button", name=re.compile(r"Generate Private Duck Address", re.I))):
                raise FlowError("duck generate button not found")
            deadline = time.time() + 15
            while time.time() < deadline:
                alias = self._read_current_alias(page)
                if alias and alias != before_click_alias:
                    previous_alias = alias
                    break
                time.sleep(0.5)
            current_alias = self._read_current_alias(page)
            previous_alias = current_alias or before_click_alias or previous_alias
            if current_alias and current_alias not in blocked:
                return current_alias, {"source": "browser_extension", "username": self.duck_username, "alias": current_alias}
            if idx < attempts and refresh_sleep_sec > 0:
                time.sleep(refresh_sleep_sec)
        if allow_profile_reset and previous_alias:
            print(f"[warn] duck alias {previous_alias} stayed blocked; resetting Duck browser profile")
            self.reset_profile()
            return self.generate_alias(
                proxies=proxies,
                exclude_aliases=blocked,
                max_refresh_attempts=max_refresh_attempts,
                refresh_sleep_sec=refresh_sleep_sec,
                allow_profile_reset=False,
            )
        raise SkipMailbox(f"duck alias did not rotate from blocked alias: {previous_alias or 'unknown'}")


def install_duck_mail_bridge(
    *,
    duck_provider: DuckTokenProvider,
    imap_client: RoutingIMAPClient,
    otp_timeout: int,
    otp_poll_interval: float,
    blocked_aliases_getter,
) -> None:
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
        blocked_aliases = set()
        try:
            blocked_aliases = set(blocked_aliases_getter() or set())
        except Exception:
            blocked_aliases = set()
        email_addr, metadata = duck_provider.generate_alias(proxies=proxies, exclude_aliases=blocked_aliases)
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
    parser.add_argument("--duck-username", default="", help="Duck username, with or without @duck.com")
    parser.add_argument("--duck-recovery-email", default="", help="Forwarding mailbox address expected in Duck sign-in emails")
    parser.add_argument("--duck-extension-path", default=str(Path.home() / ".config/google-chrome/Default/Extensions" / DUCK_EXTENSION_ID), help="Unpacked DuckDuckGo extension path or versioned parent dir")
    parser.add_argument("--duck-browser-profile-dir", default="", help="Persistent Chromium profile dir used by the Duck extension browser")
    parser.add_argument("--duck-login-timeout", type=int, default=180, help="Duck sign-in email wait timeout seconds")
    parser.add_argument("--duck-login-poll", type=float, default=3.0, help="Duck sign-in email polling interval seconds")
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

    managed_accounts = ManagedAccountStore(args.managed_accounts_file)

    imap_client = RoutingIMAPClient(
        host=args.imap_host,
        port=args.imap_port,
        username=args.imap_user,
        password=imap_password,
        folder=args.imap_folder,
        insecure=args.imap_insecure,
    )
    duck_provider = DuckExtensionProvider(
        duck_username=args.duck_username,
        extension_path=args.duck_extension_path,
        browser_profile_dir=args.duck_browser_profile_dir or str(artifacts_dir / "duck_browser_profile"),
        recovery_email=args.duck_recovery_email or args.imap_user,
        chromium_path=args.chromium_path,
        headless=args.headless,
        proxy=args.proxy,
        login_timeout=args.duck_login_timeout,
        login_poll_interval=args.duck_login_poll,
        imap_client=imap_client,
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
    print("[Info] browser duck-mail + Sub2API registrar started")
    print(f"[Info] duck account: {normalize_duck_username(args.duck_username)}@duck.com")
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
    runtime_blocked_aliases: set[str] = set()

    def blocked_duck_aliases() -> set[str]:
        blocked: set[str] = set(runtime_blocked_aliases)
        for email_addr, entry in managed_accounts.latest_accounts().items():
            if str(entry.get("source") or "").strip().lower() != "duck":
                continue
            normalized = normalize_duck_alias(email_addr)
            if normalized:
                blocked.add(normalized)
        blocked.update(load_blocked_duck_aliases_from_history(args.history_file))
        return blocked

    with sync_playwright() as pw:
        duck_provider.start(pw)
        install_duck_mail_bridge(
            duck_provider=duck_provider,
            imap_client=imap_client,
            otp_timeout=args.otp_timeout,
            otp_poll_interval=args.otp_poll,
            blocked_aliases_getter=blocked_duck_aliases,
        )
        idx = 0
        try:
            while True:
                idx += 1
                if not args.loop and idx > args.count:
                    break
                print(f"\n========== account {idx}/{args.count} ==========")
                account_done = False
                account_last_reason = ""
                current_email = ""
                alias_attempt = 0
                alias_rotation_count = 0
                while not account_done:
                    if not current_email:
                        alias_rotation_count += 1
                        alias_attempt = 0
                        try:
                            current_email, dev_token = registrar.get_email_and_token(args.proxy)
                        except Exception as exc:
                            account_last_reason = str(exc)
                            is_skip = isinstance(exc, SkipMailbox)
                            if is_skip:
                                skipped_mailboxes += 1
                                print(f"[skip] alias rotation {alias_rotation_count}: {exc}; requesting another duck mailbox")
                            else:
                                print(f"[failed] alias rotation {alias_rotation_count}: {exc}")
                            append_history(
                                args.history_file,
                                {
                                    "kind": "attempt",
                                    "at": time.time(),
                                    "success": False,
                                    "email": "",
                                    "attempt": alias_rotation_count,
                                    "error": str(exc),
                                    "skip_mailbox": is_skip,
                                    "elapsed_sec": 0.0,
                                },
                            )
                            if args.retry_sleep > 0:
                                time.sleep(args.retry_sleep)
                            continue
                        if not current_email or not dev_token:
                            raise FlowError("duck mail acquisition failed")

                    alias_attempt += 1
                    total_attempts += 1
                    print(f"[*] mailbox {current_email} attempt {alias_attempt}/{max_attempts_label}")
                    started = time.time()
                    password = secrets.token_urlsafe(18)
                    browser = None
                    context = None
                    try:
                        with attempt_deadline(args.attempt_timeout):
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
                                    email=current_email,
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
                                    email=current_email,
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
                                name=current_email,
                                group_ids=[],
                                concurrency=args.concurrency,
                                priority=args.priority,
                            )
                            account_id = int(created.get("id") or 0)
                            if account_id > 0:
                                created = post_configure_account(client, account_id=account_id, platform="openai", group_ids_raw=args.group_ids)
                        print(f"[OK] account created: id={created.get('id')}, name={created.get('name')}")
                        managed_accounts.record_duck_success(email_addr=current_email, password=password, account_id=int(created.get("id") or 0))
                        runtime_blocked_aliases.add(normalize_duck_alias(current_email))
                        append_history(
                            args.history_file,
                            {
                                "kind": "attempt",
                                "at": time.time(),
                                "success": True,
                                "email": current_email,
                                "email_domain": "duck.com",
                                "attempt": alias_attempt,
                                "account": created,
                                "elapsed_sec": round(time.time() - started, 2),
                            },
                        )
                        success += 1
                        account_done = True
                        try:
                            notifier.send(f"sub2api success\naccount={created.get('name')}\nemail={current_email}\nid={created.get('id')}\nsuccess={success}")
                        except Exception as exc:
                            print(f"[warn] telegram notification failed: {exc}")
                        append_history(
                            args.history_file,
                            {
                                "kind": "account_result",
                                "at": time.time(),
                                "success": True,
                                "account_index": idx,
                                "email": current_email,
                                "attempts_used": alias_attempt,
                                "account": created,
                            },
                        )
                        print_stats()
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
                        rotate_alias = isinstance(exc, SkipMailbox)
                        if rotate_alias:
                            skipped_mailboxes += 1
                            print(f"[skip] mailbox {current_email or 'unknown'}: {reason}; switching to next duck mailbox")
                        else:
                            print(f"[failed] mailbox {current_email} attempt {alias_attempt}: {reason}")
                        append_history(
                            args.history_file,
                            {
                                "kind": "attempt",
                                "at": time.time(),
                                "success": False,
                                "email": current_email,
                                "attempt": alias_attempt,
                                "error": reason,
                                "skip_mailbox": rotate_alias,
                                "elapsed_sec": round(time.time() - started, 2),
                            },
                        )
                        same_alias_retry = not rotate_alias and (args.max_attempts == 0 or alias_attempt < args.max_attempts)
                        if same_alias_retry:
                            if args.retry_sleep > 0:
                                time.sleep(args.retry_sleep)
                        else:
                            normalized = normalize_duck_alias(current_email)
                            if normalized:
                                runtime_blocked_aliases.add(normalized)
                            print(f"[rotate] mailbox {current_email or 'unknown'} exhausted {max_attempts_label} attempts; requesting a new duck mailbox")
                            current_email = ""
                            if args.retry_sleep > 0:
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

                if (args.loop or idx < args.count) and args.sleep > 0:
                    time.sleep(args.sleep)
        finally:
            duck_provider.close()

    total_accounts = success + failed_accounts
    print(f"\nDone: success {success}/{total_accounts or args.count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
