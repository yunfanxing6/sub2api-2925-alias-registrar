#!/usr/bin/env python3
import argparse
import email
import getpass
import imaplib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.header import decode_header, make_header
from typing import Any, Dict, Optional, Tuple

import registrar_core as registrar


OTP_REGEX = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    out: list[int] = []
    for part in text.split(","):
        p = part.strip()
        if not p:
            continue
        out.append(int(p))
    return out


class AliasStateStore:
    def __init__(
        self,
        *,
        state_path: str,
        history_path: str,
        local_prefix: str,
        domain: str,
        start_index: int,
    ) -> None:
        self.state_path = state_path
        self.history_path = history_path
        self.local_prefix = local_prefix
        self.domain = domain
        self.start_index = start_index
        self._lock = threading.Lock()
        self._ensure_state()

    def _default_state(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "local_prefix": self.local_prefix,
            "domain": self.domain,
            "next_index": self.start_index,
            "updated_at": now_iso(),
            "last_allocated": None,
        }

    def _ensure_state(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        if not os.path.exists(self.state_path):
            self._save_state(self._default_state())

    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return self._default_state()
            return data
        except Exception:
            return self._default_state()

    def _save_state(self, data: Dict[str, Any]) -> None:
        data["updated_at"] = now_iso()
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    def _append_history(self, row: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.history_path) or ".", exist_ok=True)
        line = json.dumps(row, ensure_ascii=False)
        with open(self.history_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")

    def allocate_next_alias(self) -> Tuple[str, int]:
        with self._lock:
            st = self._load_state()
            next_index = int(st.get("next_index") or self.start_index)
            if next_index < self.start_index:
                next_index = self.start_index

            email_addr = f"{self.local_prefix}{next_index}@{self.domain}"
            st["next_index"] = next_index + 1
            st["last_allocated"] = {
                "index": next_index,
                "email": email_addr,
                "at": now_iso(),
            }
            self._save_state(st)
            self._append_history(
                {
                    "at": now_iso(),
                    "event": "allocated",
                    "email": email_addr,
                    "index": next_index,
                }
            )
            return email_addr, next_index

    def record_result(
        self,
        *,
        email_addr: str,
        index: Optional[int],
        success: bool,
        detail: Dict[str, Any],
    ) -> None:
        with self._lock:
            self._append_history(
                {
                    "at": now_iso(),
                    "event": "result",
                    "success": success,
                    "email": email_addr,
                    "index": index,
                    "detail": detail,
                }
            )


def decode_header_str(raw: str) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def extract_message_text(msg: email.message.Message) -> str:
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = (part.get_content_type() or "").lower()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if content_type not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                chunks.append(payload.decode(charset, errors="replace"))
            except Exception:
                chunks.append(payload.decode("utf-8", errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            try:
                chunks.append(payload.decode(charset, errors="replace"))
            except Exception:
                chunks.append(payload.decode("utf-8", errors="replace"))
        else:
            chunks.append(str(msg.get_payload() or ""))
    return "\n".join(chunks)


class IMAP2925Client:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        folder: str,
        insecure: bool,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.folder = folder
        self.insecure = insecure

    def _connect(self) -> imaplib.IMAP4_SSL:
        ctx = ssl.create_default_context()
        if self.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ctx)
        conn.login(self.username, self.password)
        conn.select(self.folder, readonly=True)
        return conn

    def latest_uid(self) -> int:
        conn = None
        try:
            conn = self._connect()
            typ, data = conn.uid("search", None, "ALL")
            if typ != "OK" or not data or not data[0]:
                return 0
            uids = data[0].split()
            if not uids:
                return 0
            return int(uids[-1])
        except Exception:
            return 0
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass

    def wait_otp_code(
        self,
        *,
        target_email: str,
        since_uid: int,
        seen_uids: set[int],
        timeout_sec: int,
        poll_interval_sec: float,
    ) -> Tuple[str, int]:
        deadline = time.time() + max(1, timeout_sec)
        target = (target_email or "").strip().lower()

        while time.time() < deadline:
            print(".", end="", flush=True)
            conn = None
            try:
                conn = self._connect()
                typ, data = conn.uid("search", None, "ALL")
                if typ != "OK" or not data or not data[0]:
                    time.sleep(poll_interval_sec)
                    continue

                all_uids = [int(x) for x in data[0].split() if x]
                if not all_uids:
                    time.sleep(poll_interval_sec)
                    continue

                for uid in reversed(all_uids[-120:]):
                    if uid <= since_uid:
                        continue
                    if uid in seen_uids:
                        continue

                    typ_fetch, fetch_data = conn.uid("fetch", str(uid), "(RFC822)")
                    seen_uids.add(uid)
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
                    to_header = decode_header_str(msg.get("To", ""))
                    delivered_to = decode_header_str(msg.get("Delivered-To", ""))
                    orig_to = decode_header_str(msg.get("X-Original-To", ""))
                    subject = decode_header_str(msg.get("Subject", ""))
                    body = extract_message_text(msg)

                    haystack = "\n".join([sender, subject, body]).lower()
                    route_headers = "\n".join([to_header, delivered_to, orig_to]).lower()

                    if "openai" not in haystack and "openai" not in route_headers:
                        continue

                    if target and target not in route_headers:
                        # Some providers rewrite To headers. Prefer target match but do not hard-fail.
                        pass

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
        url = f"{self.base_url}{path}"
        req_headers = {"Accept": "application/json"}
        if headers:
            req_headers.update(headers)

        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url=url, data=data, method=method.upper(), headers=req_headers)
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

        # JWT mode auto-refresh once on 401
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


def install_mail_bridge(
    *,
    state_store: AliasStateStore,
    imap_client: IMAP2925Client,
    otp_timeout: int,
    otp_poll_interval: float,
    runtime_ctx: threading.local,
) -> None:
    seen_lock = threading.Lock()
    seen_by_token: Dict[str, set[int]] = {}

    def parse_token(token: str, fallback_email: str) -> Tuple[str, int, int, str]:
        # token format: imap2925:{index}:{baseline_uid}:{email}
        raw = (token or "").strip()
        if raw.startswith("imap2925:"):
            parts = raw.split(":", 3)
            if len(parts) == 4:
                idx_str, baseline_str, tok_email = parts[1], parts[2], parts[3]
                try:
                    idx = int(idx_str)
                except Exception:
                    idx = -1
                try:
                    baseline = int(baseline_str)
                except Exception:
                    baseline = 0
                em = (tok_email or fallback_email or "").strip().lower()
                return raw, idx, baseline, em
        return raw, -1, 0, (fallback_email or "").strip().lower()

    def patched_get_email_and_token(proxies: Any = None) -> tuple[str, str]:
        del proxies
        email_addr, idx = state_store.allocate_next_alias()
        baseline_uid = imap_client.latest_uid()
        token = f"imap2925:{idx}:{baseline_uid}:{email_addr}"

        runtime_ctx.current_email = email_addr
        runtime_ctx.current_index = idx
        runtime_ctx.current_dev_token = token

        print(f"[*] Using 2925 alias mailbox: {email_addr} (baseline_uid={baseline_uid})")
        return email_addr, token

    def patched_get_oai_code(
        token: str,
        email_addr: str,
        proxies: Any = None,
        seen_msg_ids: set = None,
    ) -> str:
        del proxies
        tok_key, _idx, baseline_uid, tok_email = parse_token(token, email_addr)
        target_email = (tok_email or email_addr or "").strip().lower()
        if not target_email:
            return ""

        with seen_lock:
            seen = seen_by_token.setdefault(tok_key, set())
            if seen_msg_ids:
                for item in list(seen_msg_ids):
                    try:
                        seen.add(int(str(item)))
                    except Exception:
                        pass

        print(f"[*] Waiting OTP from 2925 mailbox {target_email}", end="", flush=True)
        code, uid = imap_client.wait_otp_code(
            target_email=target_email,
            since_uid=baseline_uid,
            seen_uids=seen,
            timeout_sec=otp_timeout,
            poll_interval_sec=otp_poll_interval,
        )
        if code and uid:
            with seen_lock:
                seen.add(uid)
            if seen_msg_ids is not None:
                try:
                    seen_msg_ids.add(str(uid))
                except Exception:
                    pass
            print(f" got OTP: {code}")
            return code

        print(" timeout, no OTP received")
        return ""

    def patched_get_oai_verify(token: str, email_addr: str, proxies: Any = None) -> str:
        return patched_get_oai_code(token, email_addr, proxies=proxies)

    registrar.get_email_and_token = patched_get_email_and_token
    registrar.get_oai_code = patched_get_oai_code
    registrar.get_oai_verify = patched_get_oai_verify


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
            state=session_id,  # expected_state carries sub2api session_id
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
        alias_email = str(getattr(runtime_ctx, "current_email", "") or "").strip()

        created = client.create_from_oauth(
            session_id=session_id,
            code=code,
            state=state,
            redirect_uri=use_redirect,
            proxy_id=proxy_id,
            name=alias_email,
            group_ids=group_ids,
            concurrency=concurrency,
            priority=priority,
        )
        return json.dumps(created, ensure_ascii=False, separators=(",", ":"))

    registrar.generate_oauth_url = patched_generate_oauth_url
    registrar.submit_callback_url = patched_submit_callback_url


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Standalone registrar based on a.py with 2925 alias mailboxes and Sub2API OAuth. "
            "Aliases start from yunfanxing6500@2925.com and are persisted to avoid reuse."
        )
    )

    # Registration network
    parser.add_argument("--proxy", default=None, help="Registration proxy, e.g. http://127.0.0.1:7890")

    # Sub2API
    parser.add_argument("--sub2api-url", required=True, help="Example: https://openaiapi.icu")
    parser.add_argument("--sub2api-insecure", action="store_true", help="Skip sub2api TLS certificate validation")
    parser.add_argument("--sub2api-timeout", type=int, default=25, help="Sub2API HTTP timeout in seconds")
    parser.add_argument("--admin-api-key", default="", help="Admin x-api-key (recommended)")
    parser.add_argument("--admin-token", default="", help="Admin JWT bearer token")
    parser.add_argument("--admin-email", default="", help="Admin login email")
    parser.add_argument("--admin-password", default="", help="Admin login password")
    parser.add_argument("--login-turnstile-token", default="", help="Optional turnstile token for /auth/login")

    # Sub2API account creation settings
    parser.add_argument("--sub2api-proxy-id", type=int, default=None, help="Sub2API proxy_id to bind")
    parser.add_argument("--redirect-uri", default="http://localhost:1455/auth/callback", help="OAuth redirect_uri")
    parser.add_argument("--group-ids", default="2", help="Account group IDs, comma-separated")
    parser.add_argument("--concurrency", type=int, default=10, help="Account concurrency")
    parser.add_argument("--priority", type=int, default=1, help="Account priority")

    # 2925 mailbox settings
    parser.add_argument("--mail-login-email", default="yunfanxing6@2925.com", help="2925 IMAP login email")
    parser.add_argument("--mail-login-password", default="", help="2925 IMAP login password")
    parser.add_argument("--mail-local-prefix", default="yunfanxing6", help="Alias local-part prefix")
    parser.add_argument("--mail-domain", default="2925.com", help="Alias domain")
    parser.add_argument("--start-index", type=int, default=500, help="Start suffix index")

    # IMAP
    parser.add_argument("--imap-host", default="imap.2925.com", help="IMAP host")
    parser.add_argument("--imap-port", type=int, default=993, help="IMAP port")
    parser.add_argument("--imap-folder", default="INBOX", help="IMAP mailbox folder")
    parser.add_argument("--imap-insecure", action="store_true", help="Skip IMAP TLS certificate validation")
    parser.add_argument("--otp-timeout", type=int, default=180, help="OTP wait timeout seconds")
    parser.add_argument("--otp-poll", type=float, default=3.0, help="OTP polling interval seconds")

    # Run control
    parser.add_argument("--count", type=int, default=1, help="How many accounts to register this run")
    parser.add_argument("--sleep", type=float, default=2.0, help="Sleep seconds between accounts")

    # Persistence
    default_dir = os.path.dirname(os.path.abspath(__file__))
    parser.add_argument(
        "--state-file",
        default=os.path.join(default_dir, "2925_alias_state.json"),
        help="Alias suffix state file path",
    )
    parser.add_argument(
        "--history-file",
        default=os.path.join(default_dir, "2925_alias_history.jsonl"),
        help="Run history log file path",
    )

    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        sub2api_url = normalize_base_url(args.sub2api_url)
        group_ids = parse_group_ids(args.group_ids)
        if not args.admin_api_key and not args.admin_token and not args.admin_email:
            raise ValueError("provide --admin-api-key or --admin-token or --admin-email")
        if args.start_index < 0:
            raise ValueError("start-index must be >= 0")
        if args.count <= 0:
            raise ValueError("count must be > 0")
        if args.sub2api_timeout <= 0:
            raise ValueError("sub2api-timeout must be > 0")
        if args.otp_timeout <= 0:
            raise ValueError("otp-timeout must be > 0")
        if args.otp_poll <= 0:
            raise ValueError("otp-poll must be > 0")
    except Exception as exc:
        print(f"[config error] {exc}")
        return 2

    mail_password = args.mail_login_password
    if not mail_password:
        mail_password = getpass.getpass("2925 mailbox password: ")

    admin_password = args.admin_password
    if (
        not args.admin_api_key
        and not args.admin_token
        and args.admin_email
        and not admin_password
    ):
        admin_password = getpass.getpass("Sub2API admin password: ")

    state_store = AliasStateStore(
        state_path=args.state_file,
        history_path=args.history_file,
        local_prefix=args.mail_local_prefix,
        domain=args.mail_domain,
        start_index=args.start_index,
    )
    imap_client = IMAP2925Client(
        host=args.imap_host,
        port=args.imap_port,
        username=args.mail_login_email,
        password=mail_password,
        folder=args.imap_folder,
        insecure=args.imap_insecure,
    )
    sub2api_client = Sub2APIClient(
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

    install_mail_bridge(
        state_store=state_store,
        imap_client=imap_client,
        otp_timeout=args.otp_timeout,
        otp_poll_interval=args.otp_poll,
        runtime_ctx=runtime_ctx,
    )
    install_sub2api_bridge(
        client=sub2api_client,
        redirect_uri=args.redirect_uri,
        proxy_id=args.sub2api_proxy_id,
        group_ids=group_ids,
        concurrency=args.concurrency,
        priority=args.priority,
        runtime_ctx=runtime_ctx,
    )

    print("[Info] 2925 + Sub2API registrar started")
    print(f"[Info] state file: {args.state_file}")
    print(f"[Info] history file: {args.history_file}")

    success = 0
    for i in range(1, args.count + 1):
        print(f"\n========== account {i}/{args.count} ==========")
        started = time.time()
        runtime_ctx.oauth_round = 0
        runtime_ctx.current_email = ""
        runtime_ctx.current_index = None
        runtime_ctx.current_dev_token = ""
        current_email = ""
        current_index: Optional[int] = None

        try:
            result_json = registrar.run(args.proxy)
            current_email = str(getattr(runtime_ctx, "current_email", "") or "")
            idx_val = getattr(runtime_ctx, "current_index", None)
            if isinstance(idx_val, int):
                current_index = idx_val

            if not result_json:
                raise RuntimeError("registrar.run returned None")

            account = json.loads(result_json)
            account_id = account.get("id")
            account_name = account.get("name")
            print(f"[OK] account created: id={account_id}, name={account_name}")
            success += 1

            state_store.record_result(
                email_addr=current_email,
                index=current_index,
                success=True,
                detail={
                    "elapsed_sec": round(time.time() - started, 2),
                    "sub2api_account_id": account_id,
                    "sub2api_name": account_name,
                },
            )

        except Exception as exc:
            print(f"[failed] {exc}")
            state_store.record_result(
                email_addr=current_email,
                index=current_index,
                success=False,
                detail={
                    "elapsed_sec": round(time.time() - started, 2),
                    "error": str(exc),
                },
            )

        if i < args.count and args.sleep > 0:
            time.sleep(args.sleep)

    print(f"\nDone: success {success}/{args.count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
