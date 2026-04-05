#!/usr/bin/env python3
import argparse
import getpass
import json
import ssl
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import registrar_core as registrar


def normalize_base_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise ValueError("sub2api url is required")
    if not value.startswith("http://") and not value.startswith("https://"):
        value = "http://" + value
    return value.rstrip("/")


def parse_group_ids(raw: str) -> list[int]:
    text = (raw or "").strip()
    if not text:
        return []
    result: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if part:
            result.append(int(part))
    return result


def parse_mail_sources(raw: str) -> list[str]:
    allowed = {"tempmail_lol", "mailtm", "onesecmail", "duckmail"}
    chosen: list[str] = []
    for part in (raw or "").split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in allowed:
            raise ValueError(f"unsupported mail source: {name}")
        if name not in chosen:
            chosen.append(name)
    if not chosen:
        raise ValueError("at least one mail source is required")
    return chosen


class Sub2APIClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout: int,
        insecure: bool,
        admin_api_key: str,
        admin_token: str,
        admin_email: str,
        admin_password: str,
        login_turnstile_token: str,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.insecure = insecure
        self.admin_api_key = (admin_api_key or "").strip()
        self.admin_token = (admin_token or "").strip()
        self.admin_email = (admin_email or "").strip()
        self.admin_password = admin_password or ""
        self.login_turnstile_token = (login_turnstile_token or "").strip()
        self._jwt = ""
        self._lock = threading.Lock()

    def _http_json(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        req_headers = {"Accept": "application/json"}
        if headers:
            req_headers.update(headers)

        data = None
        if payload is not None:
            req_headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url=f"{self.base_url}{path}",
            data=data,
            method=method.upper(),
            headers=req_headers,
        )
        context = ssl._create_unverified_context() if self.insecure else None

        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=context) as resp:
                raw = resp.read().decode("utf-8", "replace").strip()
                if not raw:
                    return resp.status, {}
                parsed = json.loads(raw)
                return resp.status, parsed if isinstance(parsed, dict) else {"raw": parsed}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace").strip()
            if not raw:
                return exc.code, {"message": exc.reason or "http error"}
            try:
                parsed = json.loads(raw)
                return exc.code, parsed if isinstance(parsed, dict) else {"raw": parsed}
            except json.JSONDecodeError:
                return exc.code, {"message": raw}

    def _http_text(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, str]:
        req_headers = {"Accept": "text/plain, application/json, text/event-stream"}
        if headers:
            req_headers.update(headers)

        data = None
        if payload is not None:
            req_headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url=f"{self.base_url}{path}",
            data=data,
            method=method.upper(),
            headers=req_headers,
        )
        context = ssl._create_unverified_context() if self.insecure else None

        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=context) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    @staticmethod
    def _ok(body: Dict[str, Any]) -> bool:
        return isinstance(body, dict) and body.get("code") == 0

    @staticmethod
    def _data(body: Dict[str, Any]) -> Dict[str, Any]:
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _error_text(status: int, body: Dict[str, Any]) -> str:
        message = str(body.get("message") or "").strip()
        reason = str(body.get("reason") or "").strip()
        if message and reason:
            return f"HTTP {status} | {message} | {reason}"
        if message:
            return f"HTTP {status} | {message}"
        return f"HTTP {status}"

    def _login_jwt(self) -> str:
        payload: Dict[str, Any] = {
            "email": self.admin_email,
            "password": self.admin_password,
        }
        if self.login_turnstile_token:
            payload["turnstile_token"] = self.login_turnstile_token

        status, body = self._http_json("POST", "/api/v1/auth/login", payload=payload)
        if status != 200 or not self._ok(body):
            raise RuntimeError(f"sub2api login failed: {self._error_text(status, body)}")

        token = str(self._data(body).get("access_token") or "").strip()
        if not token:
            raise RuntimeError("sub2api login succeeded but access_token empty")
        return token

    def _auth_headers(self) -> Dict[str, str]:
        if self.admin_api_key:
            return {"x-api-key": self.admin_api_key}
        if self.admin_token:
            return {"Authorization": f"Bearer {self.admin_token}"}
        with self._lock:
            if not self._jwt:
                self._jwt = self._login_jwt()
            return {"Authorization": f"Bearer {self._jwt}"}

    def _request_admin(self, method: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = self._auth_headers()
        status, body = self._http_json(method, path, payload=payload, headers=headers)

        if (
            status == 401
            and not self.admin_api_key
            and not self.admin_token
            and self.admin_email
            and self.admin_password
        ):
            with self._lock:
                self._jwt = self._login_jwt()
                headers = {"Authorization": f"Bearer {self._jwt}"}
            status, body = self._http_json(method, path, payload=payload, headers=headers)

        if status != 200 or not self._ok(body):
            raise RuntimeError(self._error_text(status, body))
        return self._data(body)

    def _request_admin_any(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        headers = self._auth_headers()
        status, body = self._http_json(method, path, payload=payload, headers=headers)

        if (
            status == 401
            and not self.admin_api_key
            and not self.admin_token
            and self.admin_email
            and self.admin_password
        ):
            with self._lock:
                self._jwt = self._login_jwt()
                headers = {"Authorization": f"Bearer {self._jwt}"}
            status, body = self._http_json(method, path, payload=payload, headers=headers)

        if status != 200 or not self._ok(body):
            raise RuntimeError(self._error_text(status, body))

        data = body.get("data") if isinstance(body, dict) else None
        return data

    def generate_auth_url(self, *, redirect_uri: str, proxy_id: Optional[int]) -> Tuple[str, str]:
        payload: Dict[str, Any] = {"redirect_uri": redirect_uri}
        if proxy_id is not None:
            payload["proxy_id"] = proxy_id
        data = self._request_admin("POST", "/api/v1/admin/openai/generate-auth-url", payload)
        auth_url = str(data.get("auth_url") or "").strip()
        session_id = str(data.get("session_id") or "").strip()
        if not auth_url or not session_id:
            raise RuntimeError("generate-auth-url returned empty auth_url/session_id")
        return auth_url, session_id

    def create_from_oauth(
        self,
        *,
        session_id: str,
        code: str,
        state: str,
        redirect_uri: str,
        proxy_id: Optional[int],
        name: str,
        group_ids: list[int],
        concurrency: int,
        priority: int,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "session_id": session_id,
            "code": code,
            "state": state,
            "redirect_uri": redirect_uri,
            "concurrency": concurrency,
            "priority": priority,
        }
        if proxy_id is not None:
            payload["proxy_id"] = proxy_id
        if name.strip():
            payload["name"] = name.strip()
        if group_ids:
            payload["group_ids"] = group_ids
        return self._request_admin("POST", "/api/v1/admin/openai/create-from-oauth", payload)

    def list_groups_all(self, *, platform: str = "") -> List[Dict[str, Any]]:
        path = "/api/v1/admin/groups/all"
        if platform.strip():
            from urllib.parse import quote_plus
            path += f"?platform={quote_plus(platform.strip())}"
        data = self._request_admin_any("GET", path)
        return data if isinstance(data, list) else []

    def get_account(self, account_id: int) -> Dict[str, Any]:
        data = self._request_admin_any("GET", f"/api/v1/admin/accounts/{int(account_id)}")
        return data if isinstance(data, dict) else {}

    def get_available_models(self, account_id: int) -> List[Dict[str, Any]]:
        data = self._request_admin_any("GET", f"/api/v1/admin/accounts/{int(account_id)}/models")
        return data if isinstance(data, list) else []

    def update_account(self, account_id: int, updates: Dict[str, Any]) -> Dict[str, Any]:
        data = self._request_admin_any("PUT", f"/api/v1/admin/accounts/{int(account_id)}", updates)
        return data if isinstance(data, dict) else {}

    def list_accounts_page(self, *, page: int = 1, page_size: int = 100) -> Dict[str, Any]:
        query = urllib.parse.urlencode({"page": max(1, int(page)), "page_size": max(1, int(page_size))})
        data = self._request_admin_any("GET", f"/api/v1/admin/accounts?{query}")
        return data if isinstance(data, dict) else {}

    def list_accounts_all(self, *, page_size: int = 100, max_pages: int = 100) -> List[Dict[str, Any]]:
        accounts: List[Dict[str, Any]] = []
        for page in range(1, max(1, max_pages) + 1):
            payload = self.list_accounts_page(page=page, page_size=page_size)
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if isinstance(item, dict):
                    accounts.append(item)
            try:
                total = int(payload.get("total") or 0)
            except Exception:
                total = 0
            if total and len(accounts) >= total:
                break
            if len(items) < page_size:
                break
        return accounts

    def delete_account(self, account_id: int) -> None:
        path = f"/api/v1/admin/accounts/{int(account_id)}"
        headers = self._auth_headers()
        status, body = self._http_json("DELETE", path, headers=headers)

        if (
            status == 401
            and not self.admin_api_key
            and not self.admin_token
            and self.admin_email
            and self.admin_password
        ):
            with self._lock:
                self._jwt = self._login_jwt()
                headers = {"Authorization": f"Bearer {self._jwt}"}
            status, body = self._http_json("DELETE", path, headers=headers)

        if status == 204:
            return
        if status == 200 and (not isinstance(body, dict) or not body or self._ok(body)):
            return
        raise RuntimeError(self._error_text(status, body if isinstance(body, dict) else {}))

    @staticmethod
    def _parse_sse_events(raw: str) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        data_lines: List[str] = []
        for line in str(raw or "").splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
                continue
            if line.strip():
                continue
            if not data_lines:
                continue
            payload = "\n".join(data_lines).strip()
            data_lines = []
            if not payload:
                continue
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                events.append({"raw": payload})
                continue
            events.append(parsed if isinstance(parsed, dict) else {"raw": parsed})

        if data_lines:
            payload = "\n".join(data_lines).strip()
            if payload:
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    events.append({"raw": payload})
                else:
                    events.append(parsed if isinstance(parsed, dict) else {"raw": parsed})

        return events

    def test_account(self, account_id: int, *, model_id: str = "", prompt: str = "") -> Dict[str, Any]:
        path = f"/api/v1/admin/accounts/{int(account_id)}/test"
        payload: Dict[str, Any] = {}
        if model_id.strip():
            payload["model_id"] = model_id.strip()
        if prompt.strip():
            payload["prompt"] = prompt.strip()

        headers = self._auth_headers()
        headers["Accept"] = "text/event-stream"
        status, raw = self._http_text("POST", path, payload=payload, headers=headers)

        if (
            status == 401
            and not self.admin_api_key
            and not self.admin_token
            and self.admin_email
            and self.admin_password
        ):
            with self._lock:
                self._jwt = self._login_jwt()
                headers = {
                    "Authorization": f"Bearer {self._jwt}",
                    "Accept": "text/event-stream",
                }
            status, raw = self._http_text("POST", path, payload=payload, headers=headers)

        parsed_body: Dict[str, Any] = {}
        if raw.strip().startswith("{"):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                parsed_body = decoded

        return {
            "status": status,
            "ok": status == 200,
            "body": parsed_body,
            "raw": raw,
            "events": self._parse_sse_events(raw),
        }


def install_sub2api_bridge(
    *,
    client: Sub2APIClient,
    redirect_uri: str,
    proxy_id: Optional[int],
    group_ids: list[int],
    concurrency: int,
    priority: int,
    runtime_ctx: threading.local,
) -> None:
    registrar.DEFAULT_REDIRECT_URI = redirect_uri

    def patched_generate_oauth_url(
        *,
        redirect_uri: str = registrar.DEFAULT_REDIRECT_URI,
        scope: str = registrar.DEFAULT_SCOPE,
    ) -> registrar.OAuthStart:
        del scope
        use_redirect = (redirect_uri or registrar.DEFAULT_REDIRECT_URI).strip()
        round_no = int(getattr(runtime_ctx, "oauth_round", 0) or 0) + 1
        runtime_ctx.oauth_round = round_no
        phase = "register" if round_no == 1 else "login-reauthorize"
        auth_url, session_id = client.generate_auth_url(redirect_uri=use_redirect, proxy_id=proxy_id)
        print(f"[*] sub2api generate-auth-url #{round_no} ({phase})")
        return registrar.OAuthStart(
            auth_url=auth_url,
            state=session_id,
            code_verifier="sub2api-bridge",
            redirect_uri=use_redirect,
        )

    def patched_submit_callback_url(
        *,
        callback_url: str,
        expected_state: str,
        code_verifier: str,
        redirect_uri: str = registrar.DEFAULT_REDIRECT_URI,
    ) -> str:
        del code_verifier
        parsed = registrar._parse_callback_url(callback_url)
        if parsed.get("error"):
            desc = str(parsed.get("error_description") or "").strip()
            raise RuntimeError(f"oauth callback error: {parsed['error']} {desc}".strip())

        code = str(parsed.get("code") or "").strip()
        state = str(parsed.get("state") or "").strip()
        if not code:
            raise RuntimeError("callback missing code")
        if not state:
            raise RuntimeError("callback missing state")

        session_id = (expected_state or "").strip()
        if not session_id:
            raise RuntimeError("sub2api session_id missing")

        use_redirect = (redirect_uri or registrar.DEFAULT_REDIRECT_URI).strip()

        account_name = str(getattr(runtime_ctx, "current_email", "") or "").strip()
        created = client.create_from_oauth(
            session_id=session_id,
            code=code,
            state=state,
            redirect_uri=use_redirect,
            proxy_id=proxy_id,
            name=account_name,
            group_ids=group_ids,
            concurrency=concurrency,
            priority=priority,
        )
        return json.dumps(created, ensure_ascii=False, separators=(",", ":"))

    original_get_email_and_token = registrar.get_email_and_token

    def wrapped_get_email_and_token(proxies: Any = None) -> tuple[str, str]:
        email_addr, token = original_get_email_and_token(proxies)
        runtime_ctx.current_email = email_addr
        runtime_ctx.current_dev_token = token
        return email_addr, token

    registrar.generate_oauth_url = patched_generate_oauth_url
    registrar.submit_callback_url = patched_submit_callback_url
    registrar.get_email_and_token = wrapped_get_email_and_token


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Standalone registrar based on registrar_core temp mail flow and Sub2API OAuth bridge."
        )
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
    parser.add_argument("--retry-sleep", type=float, default=3.0, help="Sleep seconds between failed attempts")
    parser.add_argument("--sleep", type=float, default=2.0, help="Sleep seconds between accounts")
    parser.add_argument("--history-file", default="", help="Optional JSONL run history file")
    parser.add_argument(
        "--mail-sources",
        default="tempmail_lol,mailtm",
        help="Comma-separated temp mail sources: tempmail_lol,mailtm,onesecmail,duckmail",
    )
    parser.add_argument("--duckmail-key", default="", help="Optional DuckMail API key")
    return parser


def append_history(path: str, row: Dict[str, Any]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False))
        f.write("\n")


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        sub2api_url = normalize_base_url(args.sub2api_url)
        group_ids = parse_group_ids(args.group_ids)
        mail_sources = parse_mail_sources(args.mail_sources)
        if not args.admin_api_key and not args.admin_token and not args.admin_email:
            raise ValueError("provide --admin-api-key or --admin-token or --admin-email")
        if args.count <= 0:
            raise ValueError("count must be > 0")
        if args.max_attempts <= 0:
            raise ValueError("max-attempts must be > 0")
        if args.sub2api_timeout <= 0:
            raise ValueError("sub2api-timeout must be > 0")
        if args.concurrency <= 0:
            raise ValueError("concurrency must be > 0")
    except Exception as exc:
        print(f"[config error] {exc}")
        return 2

    admin_password = args.admin_password
    if (
        not args.admin_api_key
        and not args.admin_token
        and args.admin_email
        and not admin_password
    ):
        admin_password = getpass.getpass("Sub2API admin password: ")

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

    runtime_ctx = threading.local()
    registrar.DUCKMAIL_KEY = args.duckmail_key or ""
    registrar.MAIL_SOURCES = {
        "tempmail_lol": "tempmail_lol" in mail_sources,
        "onesecmail": "onesecmail" in mail_sources,
        "duckmail": "duckmail" in mail_sources,
        "mailtm": "mailtm" in mail_sources,
    }
    install_sub2api_bridge(
        client=client,
        redirect_uri=args.redirect_uri,
        proxy_id=args.sub2api_proxy_id,
        group_ids=group_ids,
        concurrency=args.concurrency,
        priority=args.priority,
        runtime_ctx=runtime_ctx,
    )

    print("[Info] tempmail + Sub2API registrar started")
    print(f"[Info] mail sources: {', '.join(mail_sources)}")

    success = 0
    for i in range(1, args.count + 1):
        print(f"\n========== account {i}/{args.count} ==========")
        account_started = time.time()
        account_done = False

        for attempt in range(1, args.max_attempts + 1):
            runtime_ctx.oauth_round = 0
            runtime_ctx.current_email = ""
            runtime_ctx.current_dev_token = ""
            started = time.time()
            print(f"[*] attempt {attempt}/{args.max_attempts}")

            try:
                result_json = registrar.run(args.proxy)
                email_addr = str(getattr(runtime_ctx, "current_email", "") or "")
                if not result_json:
                    raise RuntimeError("registrar.run returned None")

                account = json.loads(result_json)
                print(f"[OK] account created: id={account.get('id')}, name={account.get('name')}")
                success += 1
                account_done = True
                append_history(
                    args.history_file,
                    {
                        "at": time.time(),
                        "success": True,
                        "email": email_addr,
                        "attempt": attempt,
                        "account": account,
                        "elapsed_sec": round(time.time() - started, 2),
                        "account_elapsed_sec": round(time.time() - account_started, 2),
                    },
                )
                break
            except Exception as exc:
                email_addr = str(getattr(runtime_ctx, "current_email", "") or "")
                print(f"[failed] attempt {attempt}: {exc}")
                append_history(
                    args.history_file,
                    {
                        "at": time.time(),
                        "success": False,
                        "email": email_addr,
                        "attempt": attempt,
                        "error": str(exc),
                        "elapsed_sec": round(time.time() - started, 2),
                    },
                )
                if attempt < args.max_attempts and args.retry_sleep > 0:
                    time.sleep(args.retry_sleep)

        if not account_done:
            print(f"[failed] account {i} exhausted {args.max_attempts} attempts")

        if i < args.count and args.sleep > 0:
            time.sleep(args.sleep)

    print(f"\nDone: success {success}/{args.count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
