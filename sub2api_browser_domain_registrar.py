#!/usr/bin/env python3
import argparse
import email
import getpass
import time
from email.header import decode_header, make_header
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

import registrar_core as registrar
from sub2api_2925_alias_registrar import AliasStateStore, IMAP2925Client, OTP_REGEX, extract_message_text
from sub2api_browser_tempmail_registrar import (
    FlowError,
    NeedReauth,
    SkipMailbox,
    append_history,
    launch_context,
    perform_auth_flow,
)
from sub2api_tempmail_registrar import Sub2APIClient, normalize_base_url, parse_group_ids


def decode_header_str(raw: str) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


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
                typ, data = conn.select(self.folder, readonly=True)
                if typ != "OK" or not data or not data[0]:
                    time.sleep(poll_interval_sec)
                    continue

                latest_msg_id = int(data[0])
                if latest_msg_id <= 0:
                    time.sleep(poll_interval_sec)
                    continue

                start_msg_id = max(1, since_uid + 1, latest_msg_id - 120)
                for msg_id in range(latest_msg_id, start_msg_id - 1, -1):
                    if msg_id <= since_uid or msg_id in seen_uids:
                        continue

                    typ_fetch, fetch_data = conn.fetch(str(msg_id), "(RFC822)")
                    seen_uids.add(msg_id)
                    if typ_fetch != "OK" or not fetch_data:
                        continue

                    raw_email = b""
                    for item in fetch_data:
                        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                            raw_email = bytes(item[1])
                            break
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
                        return m.group(1), msg_id

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
    state_store: AliasStateStore,
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
        email_addr, idx = state_store.allocate_next_alias()
        baseline_uid = imap_client.latest_uid()
        token = f"domain:{baseline_uid}:{email_addr}"
        print(f"[*] domain mailbox: {email_addr} (baseline_uid={baseline_uid})")
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
    parser.add_argument("--group-ids", default="2", help="Account group IDs, comma-separated")
    parser.add_argument("--concurrency", type=int, default=10, help="Account concurrency")
    parser.add_argument("--priority", type=int, default=1, help="Account priority")
    parser.add_argument("--count", type=int, default=1, help="How many accounts to register this run")
    parser.add_argument("--max-attempts", type=int, default=5, help="Max mailbox attempts per target account")
    parser.add_argument("--retry-sleep", type=float, default=2.0, help="Sleep seconds between failed attempts")
    parser.add_argument("--sleep", type=float, default=2.0, help="Sleep seconds between accounts")
    parser.add_argument("--loop", action="store_true", help="Run continuously instead of stopping after count accounts")
    parser.add_argument("--history-file", default="", help="Optional JSONL run history file")
    parser.add_argument("--chromium-path", default="", help="Optional Chromium executable path")
    parser.add_argument("--headless", action="store_true", help="Launch Chromium in headless mode")
    parser.add_argument("--artifacts-dir", default="artifacts", help="Directory for browser profiles and screenshots")
    parser.add_argument("--mail-domain", default="xingyunfan.dpdns.org", help="Custom domain used for signup addresses")
    parser.add_argument("--mail-local-prefix", default="oc", help="Sequential local-part prefix")
    parser.add_argument("--start-index", type=int, default=1, help="Sequential alias start index")
    parser.add_argument("--state-file", default="domain_alias_state.json", help="Alias state file path")
    parser.add_argument("--alias-history-file", default="domain_alias_history.jsonl", help="Alias allocation history file path")
    parser.add_argument("--imap-host", default="imap.2925.com", help="IMAP host")
    parser.add_argument("--imap-port", type=int, default=993, help="IMAP port")
    parser.add_argument("--imap-user", default="yunfanxing6@2925.com", help="IMAP username")
    parser.add_argument("--imap-password", default="", help="IMAP password")
    parser.add_argument("--imap-folder", default="INBOX", help="IMAP mailbox folder")
    parser.add_argument("--imap-insecure", action="store_true", help="Skip IMAP TLS certificate validation")
    parser.add_argument("--otp-timeout", type=int, default=180, help="OTP wait timeout seconds")
    parser.add_argument("--otp-poll", type=float, default=3.0, help="OTP polling interval seconds")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        sub2api_url = normalize_base_url(args.sub2api_url)
        group_ids = parse_group_ids(args.group_ids)
        if not args.admin_api_key and not args.admin_token and not args.admin_email:
            raise ValueError("provide --admin-api-key or --admin-token or --admin-email")
        if args.count <= 0 or args.max_attempts <= 0:
            raise ValueError("count/max-attempts must be > 0")
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

    state_store = AliasStateStore(
        state_path=args.state_file,
        history_path=args.alias_history_file,
        local_prefix=args.mail_local_prefix,
        domain=args.mail_domain,
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

    print("[Info] browser domain-mail + Sub2API registrar started")
    print(f"[Info] signup domain: {args.mail_domain}")
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

    with sync_playwright() as pw:
        idx = 0
        while True:
            idx += 1
            if not args.loop and idx > args.count:
                break
            print(f"\n========== account {idx}/{args.count} ==========")
            account_done = False
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

                    browser, context = launch_context(
                        pw,
                        executable_path=args.chromium_path,
                        headless=args.headless,
                        artifacts_dir=str(artifacts_dir),
                    )
                    page = context.pages[0] if context.pages else context.new_page()

                    try:
                        callback_url = perform_auth_flow(
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
                        callback_url = perform_auth_flow(
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
                        group_ids=group_ids,
                        concurrency=args.concurrency,
                        priority=args.priority,
                    )
                    print(f"[OK] account created: id={created.get('id')}, name={created.get('name')}")
                    state_store.record_result(
                        email_addr=email_addr,
                        index=None,
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
                    if context is not None:
                        try:
                            page = context.pages[0] if context.pages else None
                            if page is not None:
                                print(f"[debug] page_url={page.url}")
                                print(f"[debug] page_title={page.title()}")
                                print(f"[debug] page_text={page.locator('body').inner_text(timeout=1000)[:800]}")
                        except Exception:
                            pass
                    if isinstance(exc, SkipMailbox):
                        skipped_mailboxes += 1
                        print(f"[skip] attempt {attempt}: {reason}; switching to next domain mailbox")
                    else:
                        print(f"[failed] attempt {attempt}: {reason}")
                    state_store.record_result(
                        email_addr=email_addr,
                        index=None,
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
                append_history(
                    args.history_file,
                    {
                        "kind": "account_result",
                        "at": time.time(),
                        "success": False,
                        "account_index": idx,
                        "attempts_used": args.max_attempts,
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
