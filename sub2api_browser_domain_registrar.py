#!/usr/bin/env python3
import argparse
import email
import getpass
import json
import os
import random
import string
import threading
import time
import urllib.parse
import urllib.request
from email.header import decode_header, make_header
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

import registrar_core as registrar
import sub2api_browser_tempmail_registrar as browser_flow
from sub2api_2925_alias_registrar import IMAP2925Client, OTP_REGEX, extract_message_text, now_iso
from sub2api_browser_tempmail_registrar import FlowError, NeedReauth, SkipMailbox, append_history
from sub2api_tempmail_registrar import Sub2APIClient, normalize_base_url


def decode_header_str(raw: str) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def parse_domain_list(raw: str) -> list[str]:
    domains: list[str] = []
    for part in (raw or "").split(","):
        domain = part.strip().lower()
        if not domain:
            continue
        if domain not in domains:
            domains.append(domain)
    return domains


class MultiDomainStateStore:
    def __init__(
        self,
        *,
        state_path: str,
        history_path: str,
        local_prefix: str,
        domains: list[str],
        start_index: int,
    ) -> None:
        if not domains:
            raise ValueError("at least one domain is required")
        self.state_path = state_path
        self.history_path = history_path
        self.local_prefix = local_prefix
        self.domains = domains
        self.start_index = start_index
        self._lock = threading.Lock()
        self._ensure_state()

    def _default_domain_state(self, domain: str) -> dict[str, Any]:
        return {
            "domain": domain,
            "next_index": self.start_index,
            "cooldown_until": 0.0,
            "last_allocated": None,
        }

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": 2,
            "local_prefix": self.local_prefix,
            "domain_order": self.domains,
            "next_domain_cursor": 0,
            "used_local_parts": [],
            "domains": {domain: self._default_domain_state(domain) for domain in self.domains},
            "updated_at": now_iso(),
        }

    def _append_history(self, row: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.history_path) or ".", exist_ok=True)
        with open(self.history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")

    def _save_state(self, data: dict[str, Any]) -> None:
        data["updated_at"] = now_iso()
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    def _load_state(self) -> dict[str, Any]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return self._default_state()
            return data
        except Exception:
            return self._default_state()

    def _migrate_state(self, existing: dict[str, Any]) -> dict[str, Any]:
        migrated = self._default_state()
        version = int(existing.get("version") or 1)
        if version == 1:
            old_domain = str(existing.get("domain") or "").strip().lower()
            if old_domain in migrated["domains"]:
                migrated["domains"][old_domain]["next_index"] = int(existing.get("next_index") or self.start_index)
                migrated["domains"][old_domain]["last_allocated"] = existing.get("last_allocated")
            return migrated

        stored_domains = existing.get("domains") or {}
        if isinstance(stored_domains, dict):
            for domain in self.domains:
                current = migrated["domains"][domain]
                prev = stored_domains.get(domain) or {}
                if isinstance(prev, dict):
                    try:
                        current["next_index"] = max(self.start_index, int(prev.get("next_index") or self.start_index))
                    except Exception:
                        pass
                    try:
                        current["cooldown_until"] = float(prev.get("cooldown_until") or 0.0)
                    except Exception:
                        pass
                    current["last_allocated"] = prev.get("last_allocated")
        cursor = int(existing.get("next_domain_cursor") or 0)
        if self.domains:
            migrated["next_domain_cursor"] = cursor % len(self.domains)
        used_local_parts = existing.get("used_local_parts") or []
        if isinstance(used_local_parts, list):
            migrated["used_local_parts"] = [str(x) for x in used_local_parts if str(x).strip()]
        return migrated

    def _generate_unique_local_part(self, state: dict[str, Any]) -> str:
        used = set(str(x) for x in state.get("used_local_parts") or [])
        alphabet = string.ascii_lowercase + string.digits
        for _ in range(5000):
            candidate = "".join(random.choice(alphabet) for _ in range(5))
            if candidate not in used:
                return candidate
        raise RuntimeError("failed to generate unique local part")

    def _ensure_state(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        if not os.path.exists(self.state_path):
            self._save_state(self._default_state())
            return
        current = self._load_state()
        migrated = self._migrate_state(current)
        self._save_state(migrated)

    def allocate_next_alias(self) -> tuple[str, str, int]:
        with self._lock:
            state = self._migrate_state(self._load_state())
            now_ts = time.time()
            order = state["domain_order"]
            cursor = int(state.get("next_domain_cursor") or 0) % len(order)

            chosen_domain = ""
            for offset in range(len(order)):
                domain = order[(cursor + offset) % len(order)]
                domain_state = state["domains"][domain]
                if float(domain_state.get("cooldown_until") or 0.0) <= now_ts:
                    chosen_domain = domain
                    state["next_domain_cursor"] = (cursor + offset + 1) % len(order)
                    break

            if not chosen_domain:
                earliest_domain = min(order, key=lambda d: float(state["domains"][d].get("cooldown_until") or 0.0))
                wait_seconds = max(0.0, float(state["domains"][earliest_domain].get("cooldown_until") or 0.0) - now_ts)
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                chosen_domain = earliest_domain
                state["next_domain_cursor"] = (order.index(earliest_domain) + 1) % len(order)

            domain_state = state["domains"][chosen_domain]
            next_index = int(domain_state.get("next_index") or self.start_index)
            if next_index < self.start_index:
                next_index = self.start_index
            local_part = self._generate_unique_local_part(state)
            email_addr = f"{local_part}@{chosen_domain}"
            state.setdefault("used_local_parts", []).append(local_part)
            domain_state["next_index"] = next_index + 1
            domain_state["last_allocated"] = {"email": email_addr, "index": next_index, "local_part": local_part, "at": now_iso()}
            self._save_state(state)
            self._append_history(
                {
                    "at": now_iso(),
                    "event": "allocated",
                    "domain": chosen_domain,
                    "email": email_addr,
                    "local_part": local_part,
                    "index": next_index,
                }
            )
            return email_addr, chosen_domain, next_index

    def mark_domain_cooldown(self, domain: str, seconds: float, *, reason: str) -> None:
        if not domain:
            return
        with self._lock:
            state = self._migrate_state(self._load_state())
            domain_state = state["domains"].get(domain)
            if not isinstance(domain_state, dict):
                return
            domain_state["cooldown_until"] = max(float(domain_state.get("cooldown_until") or 0.0), time.time() + max(0.0, seconds))
            self._save_state(state)
            self._append_history(
                {
                    "at": now_iso(),
                    "event": "cooldown",
                    "domain": domain,
                    "seconds": seconds,
                    "reason": reason,
                }
            )

    def record_result(self, *, email_addr: str, success: bool, detail: dict[str, Any]) -> None:
        domain = email_addr.split("@", 1)[1].lower() if "@" in email_addr else ""
        with self._lock:
            self._append_history(
                {
                    "at": now_iso(),
                    "event": "result",
                    "domain": domain,
                    "email": email_addr,
                    "success": success,
                    "detail": detail,
                }
            )


class RoutingIMAPClient(IMAP2925Client):
    def wait_otp_code(
        self,
        *,
        target_email: str,
        since_uid: int,
        seen_uids: set[int],
        timeout_sec: int,
        poll_interval_sec: float,
    ) -> tuple[str, int]:
        del target_email
        deadline = time.time() + max(1, timeout_sec)

        while time.time() < deadline:
            print(".", end="", flush=True)
            conn = None
            try:
                conn = self._connect()
                latest_seq = self._select_count(conn)
                if latest_seq <= 0:
                    time.sleep(poll_interval_sec)
                    continue

                start_seq = max(1, latest_seq - 120)
                for seq in range(latest_seq, start_seq - 1, -1):
                    uid, raw_email = self._fetch_uid_rfc822(conn, seq)
                    if uid <= 0:
                        continue
                    if uid <= since_uid or uid in seen_uids:
                        continue
                    seen_uids.add(uid)
                    if not raw_email:
                        continue

                    msg = email.message_from_bytes(raw_email)
                    sender = decode_header_str(msg.get("From", ""))
                    subject = decode_header_str(msg.get("Subject", ""))
                    body = extract_message_text(msg)
                    header_blob = "\n".join(f"{k}: {v}" for k, v in msg.items())
                    haystack = "\n".join([sender, subject, body, header_blob]).lower()
                    if "openai" not in haystack and "chatgpt" not in haystack:
                        continue

                    m = OTP_REGEX.search("\n".join([subject, body]))
                    if m:
                        return m.group(1), uid

            except Exception:
                pass
            finally:
                if conn is not None:
                    try:
                        conn.logout()
                    except Exception:
                        pass

            time.sleep(poll_interval_sec)

        return "", 0


def install_domain_mail_bridge(
    *,
    state_store: MultiDomainStateStore,
    imap_client: RoutingIMAPClient,
    otp_timeout: int,
    otp_poll_interval: float,
) -> None:
    seen_by_token: dict[str, set[int]] = {}

    def parse_token(token: str) -> tuple[str, int]:
        raw = (token or "").strip()
        parts = raw.split(":", 2)
        if len(parts) == 3 and parts[0] == "domain":
            try:
                return raw, int(parts[1])
            except Exception:
                return raw, 0
        return raw, 0

    def patched_get_email_and_token(proxies: Any = None) -> tuple[str, str]:
        del proxies
        email_addr, domain_name, idx = state_store.allocate_next_alias()
        baseline_uid = imap_client.latest_uid()
        token = f"domain:{baseline_uid}:{email_addr}"
        print(f"[*] domain mailbox: {email_addr} [{domain_name}] (baseline_uid={baseline_uid})")
        return email_addr, token

    def patched_get_oai_code(
        token: str,
        email_addr: str,
        proxies: Any = None,
        seen_msg_ids: set | None = None,
    ) -> str:
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
        description="Browser-driven custom-domain registrar using Cloudflare Email Routing + IMAP + Sub2API OAuth bridge."
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
    parser.add_argument("--max-attempts", type=int, default=5, help="Max mailbox attempts per target account")
    parser.add_argument("--retry-sleep", type=float, default=2.0, help="Sleep seconds between failed attempts")
    parser.add_argument("--sleep", type=float, default=90.0, help="Sleep seconds between accounts")
    parser.add_argument("--loop", action="store_true", help="Run continuously instead of stopping after count accounts")
    parser.add_argument("--phone-risk-threshold", type=int, default=0, help="Consecutive phone-risk failures before long cooldown")
    parser.add_argument("--phone-risk-cooldown", type=float, default=90.0, help="Cooldown seconds after repeated phone-risk failures")
    parser.add_argument("--history-file", default="", help="Optional JSONL run history file")
    parser.add_argument("--chromium-path", default="", help="Optional Chromium executable path")
    parser.add_argument("--headless", action="store_true", help="Launch Chromium in headless mode")
    parser.add_argument("--artifacts-dir", default="artifacts", help="Directory for browser profiles and screenshots")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--telegram-bot-token", default="", help="Optional Telegram bot token for notifications")
    parser.add_argument("--telegram-chat-id", default="", help="Optional Telegram chat id; auto-detected from getUpdates if empty")
    parser.add_argument("--telegram-chat-cache-file", default="telegram_chat_id.txt", help="File used to persist resolved Telegram chat id")
    parser.add_argument("--mail-domain", default="xingyunfan.dpdns.org", help="Single custom domain used for signup addresses")
    parser.add_argument("--mail-domains", default="", help="Comma-separated signup domains for round-robin use")
    parser.add_argument("--mail-local-prefix", default="oc", help="Sequential local-part prefix")
    parser.add_argument("--start-index", type=int, default=1, help="Sequential alias start index")
    parser.add_argument("--state-file", default="domain_alias_state.json", help="Alias state file path")
    parser.add_argument("--alias-history-file", default="domain_alias_history.jsonl", help="Alias allocation history file path")
    parser.add_argument("--domain-failure-cooldown", type=float, default=120.0, help="Cooldown seconds for a domain after a failed attempt")
    parser.add_argument("--imap-host", default="imap.2925.com", help="IMAP host")
    parser.add_argument("--imap-port", type=int, default=993, help="IMAP port")
    parser.add_argument("--imap-user", default="yunfanxing6@2925.com", help="IMAP username")
    parser.add_argument("--imap-password", default="", help="IMAP password")
    parser.add_argument("--imap-folder", default="INBOX", help="IMAP mailbox folder")
    parser.add_argument("--imap-insecure", action="store_true", help="Skip IMAP TLS certificate validation")
    parser.add_argument("--otp-timeout", type=int, default=180, help="OTP wait timeout seconds")
    parser.add_argument("--otp-poll", type=float, default=3.0, help="OTP polling interval seconds")
    return parser


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, cache_file: str) -> None:
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.cache_file = (cache_file or "").strip()
        if not self.chat_id and self.cache_file:
            try:
                self.chat_id = Path(self.cache_file).read_text(encoding="utf-8").strip()
            except Exception:
                pass

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    def _resolve_chat_id(self) -> str:
        if self.chat_id or not self.bot_token:
            return self.chat_id
        with urllib.request.urlopen(self._api_url("getUpdates"), timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        for item in reversed(payload.get("result") or []):
            message = item.get("message") or item.get("channel_post") or {}
            chat = message.get("chat") or {}
            chat_id = str(chat.get("id") or "").strip()
            if chat_id:
                self.chat_id = chat_id
                if self.cache_file:
                    try:
                        Path(self.cache_file).write_text(chat_id, encoding="utf-8")
                    except Exception:
                        pass
                return chat_id
        return ""

    def send(self, text: str) -> None:
        if not self.bot_token:
            return
        chat_id = self._resolve_chat_id()
        if not chat_id:
            return
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(self._api_url("sendMessage"), data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=20):
            return


def resolve_group_ids(client: Sub2APIClient, group_ids_raw: str, platform: str) -> list[int]:
    text = (group_ids_raw or "").strip().lower()
    if not text or text == "all":
        groups = client.list_groups_all(platform=platform)
        ids: list[int] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            try:
                gid = int(group.get("id"))
            except Exception:
                continue
            status = str(group.get("status") or "active").lower()
            if status != "active":
                continue
            ids.append(gid)
        return ids

    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out


def build_identity_model_mapping(models: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in models:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        mapping[model_id] = model_id
    return mapping


def post_configure_account(
    client: Sub2APIClient,
    *,
    account_id: int,
    platform: str,
    group_ids_raw: str,
) -> dict[str, Any]:
    account = client.get_account(account_id)
    credentials = dict(account.get("credentials") or {})
    group_ids = resolve_group_ids(client, group_ids_raw, platform)
    models = client.get_available_models(account_id)
    model_mapping = build_identity_model_mapping(models)
    if model_mapping:
        credentials["model_mapping"] = model_mapping

    updates: dict[str, Any] = {
        "group_ids": group_ids,
        "credentials": credentials,
    }
    return client.update_account(account_id, updates)


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        sub2api_url = normalize_base_url(args.sub2api_url)
        signup_domains = parse_domain_list(args.mail_domains) if args.mail_domains.strip() else parse_domain_list(args.mail_domain)
        if not signup_domains:
            raise ValueError("at least one signup domain is required")
        if not args.admin_api_key and not args.admin_token and not args.admin_email:
            raise ValueError("provide --admin-api-key or --admin-token or --admin-email")
        if args.count <= 0 or args.max_attempts <= 0:
            raise ValueError("count/max-attempts must be > 0")
        if args.otp_timeout <= 0 or args.otp_poll <= 0:
            raise ValueError("otp timeout/poll must be > 0")
        if args.domain_failure_cooldown < 0:
            raise ValueError("domain-failure-cooldown must be >= 0")
    except Exception as exc:
        print(f"[config error] {exc}")
        return 2

    admin_password = args.admin_password
    if not args.admin_api_key and not args.admin_token and args.admin_email and not admin_password:
        admin_password = getpass.getpass("Sub2API admin password: ")

    imap_password = args.imap_password or getpass.getpass("IMAP password: ")
    browser_flow.DEBUG_LOGS = bool(args.debug)

    artifacts_dir = Path(args.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    state_store = MultiDomainStateStore(
        state_path=args.state_file,
        history_path=args.alias_history_file,
        local_prefix=args.mail_local_prefix,
        domains=signup_domains,
        start_index=args.start_index,
    )
    imap_client = RoutingIMAPClient(
        host=args.imap_host,
        port=args.imap_port,
        username=args.imap_user,
        password=imap_password,
        folder=args.imap_folder,
        insecure=args.imap_insecure,
    )
    install_domain_mail_bridge(
        state_store=state_store,
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

    print("[Info] browser domain-mail + Sub2API registrar started")
    print(f"[Info] signup domains: {', '.join(signup_domains)}")
    print(f"[Info] imap inbox: {args.imap_user}")

    success = 0
    failed_accounts = 0
    total_attempts = 0
    skipped_mailboxes = 0
    consecutive_phone_risk = 0

    def print_stats() -> None:
        processed = success + failed_accounts
        success_rate = (success / processed * 100.0) if processed else 0.0
        attempt_success_rate = (success / total_attempts * 100.0) if total_attempts else 0.0
        print(
            "[stats] accounts=%s success=%s failed=%s success_rate=%.1f%% attempts=%s attempt_success_rate=%.1f%% skipped=%s"
            % (processed, success, failed_accounts, success_rate, total_attempts, attempt_success_rate, skipped_mailboxes)
        )

    with sync_playwright() as pw:
        idx = 0
        while True:
            idx += 1
            if not args.loop and idx > args.count:
                break
            print(f"\n========== account {idx}/{args.count} ==========")
            account_done = False
            account_force_cooldown = False
            account_last_reason = ""
            for attempt in range(1, args.max_attempts + 1):
                total_attempts += 1
                print(f"[*] attempt {attempt}/{args.max_attempts}")
                started = time.time()
                email_addr = ""
                browser = None
                context = None
                try:
                    email_addr, dev_token = registrar.get_email_and_token(args.proxy)
                    if not email_addr or not dev_token:
                        raise FlowError("domain mailbox allocation failed")
                    password = registrar.secrets.token_urlsafe(18)

                    auth_url_1, session_id_1 = client.generate_auth_url(
                        redirect_uri=args.redirect_uri,
                        proxy_id=args.sub2api_proxy_id,
                    )
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
                        auth_url_2, session_id_2 = client.generate_auth_url(
                            redirect_uri=args.redirect_uri,
                            proxy_id=args.sub2api_proxy_id,
                        )
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
                        created = post_configure_account(
                            client,
                            account_id=account_id,
                            platform="openai",
                            group_ids_raw=args.group_ids,
                        )
                    print(f"[OK] account created: id={created.get('id')}, name={created.get('name')}")
                    state_store.record_result(
                        email_addr=email_addr,
                        success=True,
                        detail={
                            "attempt": attempt,
                            "elapsed_sec": round(time.time() - started, 2),
                            "account": created,
                        },
                    )
                    append_history(
                        args.history_file,
                        {
                            "kind": "attempt",
                            "at": time.time(),
                            "success": True,
                            "email": email_addr,
                            "attempt": attempt,
                            "account": created,
                            "elapsed_sec": round(time.time() - started, 2),
                        },
                    )
                    success += 1
                    account_done = True
                    consecutive_phone_risk = 0
                    try:
                        success_domain = email_addr.split("@", 1)[1] if "@" in email_addr else ""
                        notifier.send(
                            f"sub2api success\naccount={created.get('name')}\nemail={email_addr}\ndomain={success_domain}\nid={created.get('id')}\nsuccess={success}"
                        )
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
                    reason = str(exc)
                    account_last_reason = reason
                    if context is not None:
                        try:
                            page = context.pages[0] if context.pages else None
                            if page is not None:
                                browser_flow.debug_log(f"[debug] page_url={page.url}")
                                browser_flow.debug_log(f"[debug] page_title={page.title()}")
                                browser_flow.debug_log(f"[debug] page_text={page.locator('body').inner_text(timeout=1000)[:800]}")
                        except Exception:
                            pass
                    if isinstance(exc, SkipMailbox):
                        skipped_mailboxes += 1
                        print(f"[skip] attempt {attempt}: {reason}; switching to next domain mailbox")
                    else:
                        print(f"[failed] attempt {attempt}: {reason}")

                    if reason == "phone verification still required after reauth":
                        consecutive_phone_risk += 1
                        if args.phone_risk_threshold > 0 and args.phone_risk_cooldown > 0 and consecutive_phone_risk >= args.phone_risk_threshold:
                            account_force_cooldown = True
                            print(
                                f"[warn] phone-risk streak={consecutive_phone_risk}, cooling down for {int(args.phone_risk_cooldown)}s"
                            )
                    else:
                        consecutive_phone_risk = 0

                    failed_domain = email_addr.split("@", 1)[1].lower() if "@" in email_addr else ""
                    state_store.mark_domain_cooldown(
                        failed_domain,
                        args.domain_failure_cooldown,
                        reason=reason,
                    )

                    state_store.record_result(
                        email_addr=email_addr,
                        success=False,
                        detail={
                            "attempt": attempt,
                            "error": reason,
                            "skip_mailbox": isinstance(exc, SkipMailbox),
                            "elapsed_sec": round(time.time() - started, 2),
                        },
                    )
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
                    if account_force_cooldown:
                        break
                    if attempt < args.max_attempts and args.retry_sleep > 0:
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
                print(f"[failed] account {idx} exhausted {args.max_attempts} attempts")
                failed_accounts += 1
                try:
                    failed_domain = email_addr.split("@", 1)[1] if "@" in email_addr else ""
                    notifier.send(
                        f"sub2api failed\naccount_index={idx}\nemail={email_addr or 'unknown'}\ndomain={failed_domain or 'unknown'}\nfailed_after_attempts={args.max_attempts}\nreason={account_last_reason or 'unknown'}"
                    )
                except Exception:
                    pass
                append_history(
                    args.history_file,
                    {
                        "kind": "account_result",
                        "at": time.time(),
                        "success": False,
                        "account_index": idx,
                        "attempts_used": args.max_attempts,
                        "reason": account_last_reason,
                    },
                )
                print_stats()
            sleep_seconds = args.phone_risk_cooldown if account_force_cooldown else args.sleep
            if (args.loop or idx < args.count) and sleep_seconds > 0:
                time.sleep(sleep_seconds)

    total_accounts = success + failed_accounts
    print(f"\nDone: success {success}/{total_accounts or args.count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
