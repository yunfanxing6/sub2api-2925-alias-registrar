#!/usr/bin/env python3
import argparse
import getpass
import json
import time
from pathlib import Path
from typing import Any

import sub2api_browser_tempmail_registrar as browser_flow
from managed_account_store import ManagedAccountStore, email_domain as managed_email_domain, normalize_email
from playwright.sync_api import sync_playwright
from sub2api_browser_domain_registrar import RoutingIMAPClient, account_error_text, parse_domain_list, reauthorize_domain_account
from sub2api_browser_tempmail_registrar import TelegramNotifier, append_history, attempt_deadline
from sub2api_tempmail_registrar import Sub2APIClient, normalize_base_url


TOKEN_INVALID_MARKERS = (
    "token_invalidated",
    "your authentication token has been invalidated",
    "authentication token has been invalidated",
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Continuously test one Sub2API OpenAI OAuth account at a time and repair/delete invalidated ones."
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
    parser.add_argument("--concurrency", type=int, default=10, help="Account concurrency used after reauthorization")
    parser.add_argument("--priority", type=int, default=1, help="Account priority used after reauthorization")
    parser.add_argument("--sleep", type=float, default=2.0, help="Seconds to wait after each account check")
    parser.add_argument("--min-test-interval", type=float, default=600.0, help="Skip accounts tested within this many seconds")
    parser.add_argument("--loop", action="store_true", help="Run continuously instead of stopping after one check")
    parser.add_argument("--history-file", default="", help="Optional JSONL history file")
    parser.add_argument("--state-file", default="account_health_state.json", help="State file used to track last tested account and timestamps")
    parser.add_argument("--chromium-path", default="", help="Optional Chromium executable path")
    parser.add_argument("--headless", action="store_true", help="Launch Chromium in headless mode")
    parser.add_argument("--artifacts-dir", default="artifacts", help="Directory for browser profiles and screenshots")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--telegram-bot-token", default="", help="Optional Telegram bot token for notifications")
    parser.add_argument("--telegram-chat-id", default="", help="Optional Telegram chat id; auto-detected from getUpdates if empty")
    parser.add_argument("--telegram-chat-cache-file", default="telegram_chat_id.txt", help="File used to persist resolved Telegram chat id")
    parser.add_argument("--mail-domain", default="xingyunfan.dpdns.org", help="Single custom domain used for signup addresses")
    parser.add_argument("--mail-domains", default="", help="Comma-separated signup domains for round-robin use")
    parser.add_argument("--imap-host", default="imap.2925.com", help="IMAP host")
    parser.add_argument("--imap-port", type=int, default=993, help="IMAP port")
    parser.add_argument("--imap-user", default="yunfanxing6@2925.com", help="IMAP username")
    parser.add_argument("--imap-password", default="", help="IMAP password")
    parser.add_argument("--imap-folder", default="INBOX", help="IMAP mailbox folder")
    parser.add_argument("--imap-insecure", action="store_true", help="Skip IMAP TLS certificate validation")
    parser.add_argument("--managed-accounts-file", default="managed_account_registry.jsonl", help="JSONL file used to track managed account metadata")
    parser.add_argument("--test-model-id", default="", help="Optional model_id for POST /accounts/{id}/test")
    parser.add_argument("--test-prompt", default="", help="Optional prompt for POST /accounts/{id}/test")
    parser.add_argument("--page-size", type=int, default=100, help="Account list page size")
    parser.add_argument("--max-pages", type=int, default=100, help="Maximum pages when listing accounts")
    parser.add_argument("--reauthorize-timeout", type=float, default=600.0, help="Hard timeout seconds for one domain reauthorization")
    return parser


def compact_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(value)


def is_openai_oauth_account(account: dict[str, Any]) -> bool:
    return (
        isinstance(account, dict)
        and str(account.get("platform") or "").strip().lower() == "openai"
        and str(account.get("type") or "").strip().lower() == "oauth"
    )


def is_token_invalidated_text(text: str) -> bool:
    haystack = str(text or "").strip().lower()
    return any(marker in haystack for marker in TOKEN_INVALID_MARKERS)


def build_test_trace(test_result: dict[str, Any], account: dict[str, Any]) -> str:
    parts = [
        str(test_result.get("raw") or ""),
        compact_json(test_result.get("events") or []),
        compact_json(test_result.get("body") or {}),
        account_error_text(account),
    ]
    return "\n".join(part for part in parts if part)


def test_single_account(
    client: Sub2APIClient,
    *,
    account: dict[str, Any],
    model_id: str,
    prompt: str,
) -> dict[str, Any]:
    account_id = int(account.get("id") or 0)
    email_addr = normalize_email(str(account.get("name") or ""))
    test_result: dict[str, Any] = {}
    refreshed = dict(account)
    worker_error = ""
    try:
        test_result = client.test_account(account_id, model_id=model_id, prompt=prompt)
    except Exception as exc:
        worker_error = str(exc)
        test_result = {
            "status": 0,
            "ok": False,
            "body": {},
            "raw": worker_error,
            "events": [],
        }
    try:
        refreshed = client.get_account(account_id)
    except Exception as exc:
        if not worker_error:
            worker_error = str(exc)
    trace_text = build_test_trace(test_result, refreshed)
    test_complete = False
    test_success = False
    for event in test_result.get("events") or []:
        if not isinstance(event, dict):
            continue
        if str(event.get("type") or "") == "test_complete":
            test_complete = True
            test_success = bool(event.get("success"))
    return {
        "account_id": account_id,
        "email": email_addr,
        "account": account,
        "refreshed": refreshed,
        "test_result": test_result,
        "worker_error": worker_error,
        "test_complete": test_complete,
        "test_success": test_success,
        "token_invalidated": is_token_invalidated_text(trace_text),
        "trace_text": trace_text,
    }


def notify_safe(notifier: TelegramNotifier, text: str) -> None:
    try:
        notifier.send(text)
    except Exception:
        pass


def delete_account_and_record(
    *,
    client: Sub2APIClient,
    history_file: str,
    account_id: int,
    email_addr: str,
    reason: str,
    error_text: str,
    action: str,
) -> None:
    client.delete_account(account_id)
    append_history(
        history_file,
        {
            "kind": "account_health_action",
            "at": time.time(),
            "success": True,
            "action": action,
            "deleted_account_id": account_id,
            "email": email_addr,
            "reason": reason,
            "error": error_text,
        },
    )


def handle_invalidated_account(
    pw,
    *,
    client: Sub2APIClient,
    imap_client: RoutingIMAPClient,
    managed_accounts: ManagedAccountStore,
    signup_domains: set[str],
    test_info: dict[str, Any],
    redirect_uri: str,
    sub2api_proxy_id: int | None,
    chromium_path: str,
    headless: bool,
    artifacts_dir: str,
    proxy: str | None,
    concurrency: int,
    priority: int,
    group_ids_raw: str,
    history_file: str,
    notifier: TelegramNotifier,
    reauthorize_timeout: float,
) -> dict[str, int]:
    account_id = int(test_info.get("account_id") or 0)
    email_addr = normalize_email(str(test_info.get("email") or ""))
    known_accounts = managed_accounts.latest_accounts()
    entry = dict(known_accounts.get(email_addr) or {})
    domain = managed_email_domain(email_addr)
    error_text = account_error_text(test_info.get("refreshed") or test_info.get("account") or {}) or str(test_info.get("trace_text") or "")[:4000]
    source = str(entry.get("source") or "").strip().lower()
    is_domain_account = source == "domain" or domain in signup_domains
    is_duck_account = source == "duck"
    is_managed_account = bool(entry) or domain in signup_domains
    password = str(entry.get("password") or "")

    if not email_addr or "@" not in email_addr or not is_managed_account:
        print(f"[health] skipping unmanaged invalidated account id={account_id} name={email_addr or test_info.get('email')}")
        append_history(
            history_file,
            {
                "kind": "account_health_action",
                "at": time.time(),
                "success": False,
                "action": "skip_unmanaged_invalidated",
                "account_id": account_id,
                "email": email_addr or str(test_info.get("email") or ""),
                "error": error_text,
            },
        )
        return {"reauthorized": 0, "deleted": 0, "action_failed": 0, "skipped": 1}

    if (is_domain_account or is_duck_account) and password:
        account_kind = "duck" if is_duck_account else "domain"
        print(f"[health] token invalidated, reauthorizing {account_kind} account id={account_id} email={email_addr}")
        client.delete_account(account_id)
        try:
            with attempt_deadline(reauthorize_timeout):
                created = reauthorize_domain_account(
                    pw,
                    client=client,
                    imap_client=imap_client,
                    email_addr=email_addr,
                    password=password,
                    proxy=proxy,
                    redirect_uri=redirect_uri,
                    sub2api_proxy_id=sub2api_proxy_id,
                    chromium_path=chromium_path,
                    headless=headless,
                    artifacts_dir=artifacts_dir,
                    concurrency=concurrency,
                    priority=priority,
                    group_ids_raw=group_ids_raw,
                )
            if is_duck_account:
                managed_accounts.record_duck_success(
                    email_addr=email_addr,
                    password=password,
                    account_id=int(created.get("id") or 0),
                )
            else:
                managed_accounts.record_domain_success(
                    email_addr=email_addr,
                    domain=domain,
                    password=password,
                    account_id=int(created.get("id") or 0),
                )
            append_history(
                history_file,
                {
                    "kind": "account_health_action",
                    "at": time.time(),
                    "success": True,
                    "action": f"reauthorize_{account_kind}_invalidated",
                    "deleted_account_id": account_id,
                    "email": email_addr,
                    "account": created,
                    "error": error_text,
                },
            )
            notify_safe(
                notifier,
                f"sub2api reauthorized\naction=reauthorize_{account_kind}_invalidated\nemail={email_addr}\nold_id={account_id}\nnew_id={created.get('id')}",
            )
            return {"reauthorized": 1, "deleted": 0, "action_failed": 0, "skipped": 0}
        except Exception as exc:
            append_history(
                history_file,
                {
                    "kind": "account_health_action",
                    "at": time.time(),
                    "success": False,
                    "action": f"reauthorize_{account_kind}_invalidated",
                    "deleted_account_id": account_id,
                    "email": email_addr,
                    "reason": str(exc),
                    "error": error_text,
                },
            )
            notify_safe(
                notifier,
                f"sub2api reauthorize failed\naction=reauthorize_{account_kind}_invalidated\nemail={email_addr}\nold_id={account_id}\nreason={str(exc)[:200]}",
            )
            return {"reauthorized": 0, "deleted": 1, "action_failed": 1, "skipped": 0}

    delete_reason = "missing_saved_password" if (is_domain_account or is_duck_account) else "tempmail_invalidated"
    delete_action = (
        "delete_duck_invalidated"
        if is_duck_account
        else "delete_domain_invalidated"
        if is_domain_account
        else "delete_tempmail_invalidated"
    )
    print(f"[health] deleting invalidated account id={account_id} email={email_addr} reason={delete_reason}")
    delete_account_and_record(
        client=client,
        history_file=history_file,
        account_id=account_id,
        email_addr=email_addr,
        reason=delete_reason,
        error_text=error_text,
        action=delete_action,
    )
    notify_safe(
        notifier,
        f"sub2api deleted invalidated\nemail={email_addr}\nid={account_id}\nreason={delete_reason}",
    )
    return {"reauthorized": 0, "deleted": 1, "action_failed": 0, "skipped": 0}


def load_state(path: Path) -> dict[str, Any]:
    default = {"version": 1, "cursor_key": "", "tested_at": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    if not isinstance(raw, dict):
        return default
    tested_at = raw.get("tested_at") if isinstance(raw.get("tested_at"), dict) else {}
    normalized_tested_at: dict[str, float] = {}
    for key, value in tested_at.items():
        try:
            normalized_tested_at[str(key)] = float(value)
        except Exception:
            continue
    return {
        "version": 1,
        "cursor_key": str(raw.get("cursor_key") or ""),
        "tested_at": normalized_tested_at,
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def account_state_key(account: dict[str, Any]) -> str:
    email_addr = normalize_email(str(account.get("name") or ""))
    if email_addr and "@" in email_addr:
        return email_addr
    account_id = int(account.get("id") or 0)
    return f"id:{account_id}" if account_id > 0 else email_addr or "unknown"


def prune_tested_at(state: dict[str, Any], *, now_ts: float, keep_seconds: float) -> None:
    tested_at = state.get("tested_at")
    if not isinstance(tested_at, dict):
        state["tested_at"] = {}
        return
    cutoff = now_ts - max(keep_seconds, 0.0)
    stale = [key for key, value in tested_at.items() if float(value or 0.0) < cutoff]
    for key in stale:
        tested_at.pop(key, None)


def select_next_account(accounts: list[dict[str, Any]], state: dict[str, Any], *, min_test_interval: float, now_ts: float) -> tuple[dict[str, Any] | None, str, float]:
    ordered = [account for account in reversed(accounts) if is_openai_oauth_account(account)]
    if not ordered:
        return None, "", max(1.0, min_test_interval)

    keys = [account_state_key(account) for account in ordered]
    cursor_key = str(state.get("cursor_key") or "")
    start_index = 0
    if cursor_key in keys:
        start_index = (keys.index(cursor_key) + 1) % len(keys)

    shortest_wait = max(1.0, min_test_interval)
    for offset in range(len(ordered)):
        idx = (start_index + offset) % len(ordered)
        account = ordered[idx]
        key = keys[idx]
        last_tested_at = float((state.get("tested_at") or {}).get(key) or 0.0)
        wait_for = max(0.0, float(min_test_interval) - max(0.0, now_ts - last_tested_at))
        if wait_for <= 0:
            return account, key, 0.0
        shortest_wait = min(shortest_wait, wait_for)
    return None, "", shortest_wait


def run_iteration(
    pw,
    *,
    args: argparse.Namespace,
    client: Sub2APIClient,
    imap_client: RoutingIMAPClient,
    managed_accounts: ManagedAccountStore,
    notifier: TelegramNotifier,
    state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], float]:
    started = time.time()
    all_accounts = client.list_accounts_all(page_size=args.page_size, max_pages=args.max_pages)
    accounts = [account for account in all_accounts if is_openai_oauth_account(account)]
    signup_domains = {item.strip().lower() for item in args.signup_domains if item.strip()}

    now_ts = time.time()
    account, state_key, wait_hint = select_next_account(
        accounts,
        state,
        min_test_interval=args.min_test_interval,
        now_ts=now_ts,
    )
    if account is None:
        summary = {
            "kind": "account_health_idle",
            "at": now_ts,
            "accounts": len(accounts),
            "wait_sec": round(wait_hint, 2),
        }
        print(f"[health] no due account, idle for {wait_hint:.1f}s")
        append_history(args.history_file, summary)
        return summary, state, max(1.0, wait_hint)

    info = test_single_account(
        client,
        account=account,
        model_id=args.test_model_id,
        prompt=args.test_prompt,
    )
    checked_at = time.time()
    state["cursor_key"] = state_key
    state.setdefault("tested_at", {})[state_key] = checked_at
    prune_tested_at(state, now_ts=checked_at, keep_seconds=max(args.min_test_interval * 3, 86400.0))
    save_state(args.state_file_path, state)

    summary = {
        "kind": "account_health_check",
        "at": checked_at,
        "account_id": info.get("account_id"),
        "email": info.get("email"),
        "test_complete": info.get("test_complete"),
        "test_success": info.get("test_success"),
        "token_invalidated": info.get("token_invalidated"),
        "worker_error": info.get("worker_error") or "",
        "elapsed_sec": round(time.time() - started, 2),
    }

    print(f"[health] checked id={info.get('account_id')} email={info.get('email')} invalidated={info.get('token_invalidated')}")

    if info.get("token_invalidated"):
        outcome = handle_invalidated_account(
            pw,
            client=client,
            imap_client=imap_client,
            managed_accounts=managed_accounts,
            signup_domains=signup_domains,
            test_info=info,
            redirect_uri=args.redirect_uri,
            sub2api_proxy_id=args.sub2api_proxy_id,
            chromium_path=args.chromium_path,
            headless=args.headless,
            artifacts_dir=str(args.artifacts_dir_path),
            proxy=args.proxy,
            concurrency=args.concurrency,
            priority=args.priority,
            group_ids_raw=args.group_ids,
            history_file=args.history_file,
            notifier=notifier,
            reauthorize_timeout=args.reauthorize_timeout,
        )
        summary.update(outcome)
    append_history(args.history_file, summary)
    return summary, state, max(0.0, float(args.sleep))


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
        if args.sub2api_timeout <= 0:
            raise ValueError("sub2api-timeout must be > 0")
        if args.page_size <= 0 or args.max_pages <= 0:
            raise ValueError("page-size/max-pages must be > 0")
        if args.sleep < 0:
            raise ValueError("sleep must be >= 0")
        if args.min_test_interval <= 0:
            raise ValueError("min-test-interval must be > 0")
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
    args.artifacts_dir_path = artifacts_dir
    args.state_file_path = Path(args.state_file)
    args.signup_domains = signup_domains

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
    imap_client = RoutingIMAPClient(
        host=args.imap_host,
        port=args.imap_port,
        username=args.imap_user,
        password=imap_password,
        folder=args.imap_folder,
        insecure=args.imap_insecure,
    )
    managed_accounts = ManagedAccountStore(args.managed_accounts_file)
    notifier = TelegramNotifier(args.telegram_bot_token, args.telegram_chat_id, args.telegram_chat_cache_file)
    state = load_state(args.state_file_path)

    print("[Info] sub2api account health monitor started")
    print(f"[Info] signup domains: {', '.join(signup_domains)}")
    print(f"[Info] min test interval: {int(args.min_test_interval)}s")

    with sync_playwright() as pw:
        iteration = 0
        while True:
            iteration += 1
            print(f"\n========== health iteration {iteration} ==========")
            _, state, next_sleep = run_iteration(
                pw,
                args=args,
                client=client,
                imap_client=imap_client,
                managed_accounts=managed_accounts,
                notifier=notifier,
                state=state,
            )
            if not args.loop:
                break
            if next_sleep > 0:
                time.sleep(next_sleep)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
