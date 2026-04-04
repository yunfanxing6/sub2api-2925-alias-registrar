#!/usr/bin/env python3
import argparse
import getpass
import json
import random
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from managed_account_store import ManagedAccountStore, email_domain
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

import registrar_core as registrar
from sub2api_tempmail_registrar import Sub2APIClient, normalize_base_url, parse_mail_sources


DEBUG_LOGS = False


def debug_log(*parts: object) -> None:
    if DEBUG_LOGS:
        print(*parts)


def random_identity() -> tuple[str, str, str, str, str]:
    first_names = ["James", "Mary", "John", "Emma", "Robert", "Sarah", "David", "Laura", "Michael", "Anna"]
    last_names = ["Smith", "Brown", "Wilson", "Taylor", "Clark", "Hall", "Lewis", "Young", "King", "Green"]
    first = random.choice(first_names)
    last = random.choice(last_names)
    year = str(random.randint(1990, 2004))
    month = str(random.randint(1, 12)).zfill(2)
    day = str(random.randint(1, 28)).zfill(2)
    return first, last, year, month, day


class FlowError(RuntimeError):
    pass


class NeedReauth(RuntimeError):
    pass


class SkipMailbox(RuntimeError):
    pass


def is_retryable_cert_error(exc: Exception) -> bool:
    return "ERR_CERT_VERIFIER_CHANGED" in str(exc)


def goto_with_retry(page: Page, url: str, *, attempts: int = 3) -> None:
    last_exc = None
    for idx in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
            return
        except Exception as exc:
            last_exc = exc
            if not is_retryable_cert_error(exc) or idx >= attempts:
                raise
            print(f"[warn] retrying navigation after cert verifier change ({idx}/{attempts})")
            time.sleep(3)
            try:
                page.context.clear_cookies()
            except Exception:
                pass
    if last_exc is not None:
        raise last_exc


def visible_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=2000)
    except Exception:
        return ""


def maybe_click(locator) -> bool:
    try:
        if locator.count() > 0 and locator.first.is_visible():
            locator.first.click(timeout=3000)
            return True
    except Exception:
        return False
    return False


def maybe_fill(locator, value: str) -> bool:
    try:
        if locator.count() > 0 and locator.first.is_visible():
            locator.first.click(timeout=3000)
            locator.first.fill(value, timeout=3000)
            return True
    except Exception:
        return False
    return False


def maybe_type_widget(page: Page, locator, value: str) -> bool:
    try:
        if locator.count() > 0 and locator.first.is_visible():
            locator.first.evaluate(
                """
                el => {
                  el.focus();
                  const sel = window.getSelection();
                  const range = document.createRange();
                  range.selectNodeContents(el);
                  sel.removeAllRanges();
                  sel.addRange(range);
                }
                """
            )
            page.keyboard.type(value, delay=50)
            return True
    except Exception:
        return False
    return False


def click_turnstile_if_present(page: Page) -> bool:
    clicked = False
    for frame in page.frames:
        try:
            checkbox = frame.get_by_role("checkbox")
            if checkbox.count() > 0 and checkbox.first.is_visible():
                checkbox.first.click(timeout=3000)
                clicked = True
                continue
        except Exception:
            pass
        try:
            checkbox = frame.locator("input[type='checkbox']")
            if checkbox.count() > 0 and checkbox.first.is_visible():
                checkbox.first.check(timeout=3000, force=True)
                clicked = True
                continue
        except Exception:
            pass
    return clicked


def wait_cloudflare(page: Page, timeout_sec: int = 60) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        current_url = page.url
        if "localhost" in current_url and "code=" in current_url:
            return
        text = visible_text(page)
        if not any(token in text for token in ["执行安全验证", "Checking your Browser", "Just a moment", "确认您是真人"]):
            return
        click_turnstile_if_present(page)
        time.sleep(2)
    raise FlowError("cloudflare challenge not cleared")


def find_email_box(page: Page):
    locators = [
        page.get_by_role("textbox", name=re.compile(r"电子邮件|email", re.I)),
        page.locator("input[type='email']"),
        page.locator("input[name*='email' i]"),
        page.locator("input[autocomplete='email']"),
    ]
    for loc in locators:
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            pass
    return None


def wait_for_email_box(page: Page, timeout_sec: int = 20):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        box = find_email_box(page)
        if box is not None:
            return box
        time.sleep(1)
    return None


def find_password_box(page: Page):
    locators = [
        page.locator("input[type='password']"),
        page.locator("input[name*='password' i]"),
        page.locator("input[autocomplete='current-password'], input[autocomplete='new-password']"),
        page.get_by_role("textbox", name=re.compile(r"密码|password", re.I)),
    ]
    for loc in locators:
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            pass
    return None


def click_continue(page: Page) -> bool:
    buttons = [
        page.get_by_role("button", name=re.compile(r"继续|continue|next|下一步|创建帐户|创建账户|注册|登录", re.I)),
        page.locator("button[type='submit']"),
    ]
    for btn in buttons:
        if maybe_click(btn):
            return True
    return False


def fill_otp(page: Page, otp_code: str) -> bool:
    if not otp_code:
        return False
    inputs = page.locator("input[inputmode='numeric'], input[autocomplete='one-time-code'], input[maxlength='1']")
    try:
        count = inputs.count()
    except Exception:
        count = 0
    if count >= 6:
        try:
            for idx, ch in enumerate(otp_code[:6]):
                inputs.nth(idx).fill(ch, timeout=2000)
            return True
        except Exception:
            pass
    for loc in [
        page.get_by_role("textbox", name=re.compile(r"代码|code|验证码", re.I)),
        page.locator("input[inputmode='numeric']"),
        page.locator("input[type='text']"),
        page.locator("[contenteditable='true']"),
    ]:
        if maybe_fill(loc, otp_code):
            return True

    try:
        page.locator("body").click(timeout=2000)
        page.keyboard.type(otp_code, delay=50)
        return True
    except Exception:
        pass

    try:
        page.evaluate(
            """
            (value) => {
              const candidates = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]'))
              for (const el of candidates) {
                if (!(el instanceof HTMLElement)) continue
                const style = window.getComputedStyle(el)
                if (style.display === 'none' || style.visibility === 'hidden') continue
                if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
                  el.focus()
                  el.value = value
                  el.dispatchEvent(new Event('input', { bubbles: true }))
                  el.dispatchEvent(new Event('change', { bubbles: true }))
                  return true
                }
                if (el.isContentEditable) {
                  el.focus()
                  el.textContent = value
                  el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }))
                  el.dispatchEvent(new Event('change', { bubbles: true }))
                  return true
                }
              }
              return false
            }
            """,
            otp_code,
        )
        return True
    except Exception:
        pass
    return False


def complete_profile(page: Page) -> None:
    first, last, year, month, day = random_identity()
    birthday = f"{year}-{month}-{day}"
    age_value = str(random.randint(22, 34))
    text = visible_text(page)
    if not any(token in text.lower() for token in ["about", "tell us", "名字", "姓", "birth", "生日", "about you", "年龄", "age"]):
        return

    try:
        debug_log("[debug] profile inputs=", page.locator("input").evaluate_all(
            "els => els.map(e => ({type:e.type, name:e.name, placeholder:e.placeholder, aria:e.getAttribute('aria-label'), value:e.value}))"
        ))
        debug_log("[debug] profile widgets=", page.locator("input, button, select, [role='combobox'], [role='spinbutton']").evaluate_all(
            "els => els.map(e => ({tag:e.tagName, type:e.getAttribute('type'), name:e.getAttribute('name'), text:(e.innerText||e.textContent||'').trim().slice(0,80), aria:e.getAttribute('aria-label'), value:e.value||''}))"
        ))
        form = page.locator("form")
        if form.count() > 0:
            debug_log("[debug] profile form html=", form.first.evaluate("el => el.outerHTML.slice(0, 5000)"))
    except Exception:
        pass

    full_name = f"{first} {last}"
    first_done = maybe_fill(page.get_by_label(re.compile(r"全名|name|名|first", re.I)), full_name)
    last_done = False

    if not first_done:
        txts = page.locator("input[type='text']")
        try:
            if txts.count() >= 1 and txts.nth(0).is_visible():
                txts.nth(0).fill(full_name, timeout=3000)
                first_done = True
        except Exception:
            pass

    maybe_fill(page.get_by_label(re.compile(r"年|year", re.I)), year)
    maybe_fill(page.get_by_label(re.compile(r"月|month", re.I)), month)
    maybe_fill(page.get_by_label(re.compile(r"日|day", re.I)), day)
    maybe_fill(page.get_by_label(re.compile(r"年龄|age", re.I)), age_value)
    maybe_fill(page.locator("input[name*='year' i]"), year)
    maybe_fill(page.locator("input[name*='month' i]"), month)
    maybe_fill(page.locator("input[name*='day' i], input[name*='date' i], input[name*='birth' i]"), day)
    maybe_fill(page.locator("input[name*='age' i]"), age_value)

    _ = (
        maybe_type_widget(page, page.locator("[data-type='year']"), year)
        or maybe_type_widget(page, page.get_by_role("spinbutton", name=re.compile(r"年|year", re.I)), year)
        or maybe_type_widget(page, page.locator("[aria-label^='年']"), year)
    )
    _ = (
        maybe_type_widget(page, page.locator("[data-type='month']"), month)
        or maybe_type_widget(page, page.get_by_role("spinbutton", name=re.compile(r"月|month", re.I)), month)
        or maybe_type_widget(page, page.locator("[aria-label^='月']"), month)
    )
    _ = (
        maybe_type_widget(page, page.locator("[data-type='day']"), day)
        or maybe_type_widget(page, page.get_by_role("spinbutton", name=re.compile(r"日|day", re.I)), day)
        or maybe_type_widget(page, page.locator("[aria-label^='日']"), day)
    )
    _ = (
        maybe_type_widget(page, page.get_by_role("spinbutton", name=re.compile(r"年龄|age", re.I)), age_value)
        or maybe_type_widget(page, page.locator("[aria-label^='年龄']"), age_value)
    )

    try:
        selects = page.locator("select")
        if selects.count() >= 3:
            selects.nth(0).select_option(year)
            selects.nth(1).select_option(str(int(month)))
            selects.nth(2).select_option(str(int(day)))
            debug_log(f"[debug] birthday selects set to {birthday}")
    except Exception:
        pass

    try:
        page.evaluate(
            """
            ({ fullName, birthday, ageValue }) => {
              const setValue = (el, value) => {
                if (!el) return;
                const proto = Object.getPrototypeOf(el);
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) {
                  desc.set.call(el, value);
                } else {
                  el.value = value;
                }
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
              };

              setValue(document.querySelector('input[name="name"]'), fullName);
              setValue(document.querySelector('input[name="age"]'), ageValue);
              setValue(document.querySelector('input[name="birthday"]'), birthday);
              const form = document.querySelector('form');
              if (form) {
                form.dispatchEvent(new Event('input', { bubbles: true }));
                form.dispatchEvent(new Event('change', { bubbles: true }));
              }
            }
            """,
            {
                "fullName": full_name,
                "birthday": birthday,
                "ageValue": age_value,
            },
        )
    except Exception:
        pass

    try:
        birthday_input = page.locator("input[name='birthday']")
        if birthday_input.count() > 0:
            birthday_input.evaluate(
                "(el, value) => { el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }",
                birthday,
            )
            debug_log(f"[debug] birthday set to {birthday}")
            try:
                debug_log(f"[debug] birthday current hidden={birthday_input.input_value(timeout=1000)}")
            except Exception:
                pass
    except Exception:
        pass

    try:
        age_input = page.locator("input[name='age']")
        if age_input.count() > 0:
            debug_log(f"[debug] age current={age_input.input_value(timeout=1000)}")
    except Exception:
        pass

    try:
        txts = page.locator("input")
        visible = []
        for idx in range(txts.count()):
            item = txts.nth(idx)
            if item.is_visible():
                visible.append(item)
        if len(visible) >= 4:
            if not first_done:
                visible[0].fill(full_name, timeout=3000)
            visible[1].fill(year, timeout=3000)
            visible[2].fill(month, timeout=3000)
            visible[3].fill(day, timeout=3000)
    except Exception:
        pass

    if not maybe_click(page.get_by_role("button", name=re.compile(r"完成帐户创建|create account|finish", re.I))):
        click_continue(page)


def detect_phone_challenge(page: Page) -> bool:
    text = visible_text(page).lower()
    return any(token in text for token in ["phone", "手机号", "手机号码", "verify your identity", "验证您的身份"]) \
        or "phone" in page.url.lower()


def detect_unsupported_email(page: Page) -> bool:
    text = visible_text(page).lower()
    return any(token in text for token in ["unsupported_email", "not supported", "不受支持", "不支持该邮箱"]) \
        or "unsupported_email" in page.url.lower()


def is_email_otp_page(page: Page) -> bool:
    text = visible_text(page).lower()
    url = page.url.lower()
    return (
        "email-verification" in url
        or any(
            token in text
            for token in [
                "验证码",
                "check your inbox",
                "检查您的收件箱",
                "输入我们刚刚向",
                "email verification",
                "one-time code",
                "one time code",
            ]
        )
    )


def is_codex_consent_page(page: Page) -> bool:
    text = visible_text(page).lower()
    url = page.url.lower()
    return (
        "/consent" in url
        or "登录到 codex" in text
        or "chatgpt 将向 codex 提供" in text
        or "sign in to codex" in text
    )


def maybe_click_resend_email(page: Page) -> bool:
    return maybe_click(page.get_by_role("button", name=re.compile(r"重新发送|resend", re.I))) or maybe_click(
        page.get_by_role("link", name=re.compile(r"重新发送|resend", re.I))
    )


def wait_for_callback(page: Page, redirect_uri: str, timeout_sec: int = 90, requested_callback: Optional[dict[str, str]] = None) -> Optional[str]:
    deadline = time.time() + timeout_sec
    redirect_prefix = redirect_uri.split("?")[0]
    clicked_consent = False
    while time.time() < deadline:
        current_url = page.url
        if current_url.startswith(redirect_prefix) and "code=" in current_url:
            return current_url
        if "localhost" in current_url and "code=" in current_url:
            return current_url
        if requested_callback and requested_callback.get("url"):
            return requested_callback["url"]
        if is_codex_consent_page(page):
            if not clicked_consent:
                if maybe_click(page.get_by_role("button", name=re.compile(r"继续|continue|allow", re.I))):
                    clicked_consent = True
                    time.sleep(3)
                    continue
        if detect_unsupported_email(page):
            raise SkipMailbox("unsupported_email")
        if detect_phone_challenge(page):
            raise NeedReauth("phone verification requested")
        time.sleep(1)
    return None


def ensure_signup_page(page: Page) -> None:
    text = visible_text(page)
    if "欢迎回来" in text or "welcome back" in text.lower():
        maybe_click(page.get_by_role("link", name=re.compile(r"注册|sign up|create account", re.I)))
        time.sleep(2)


def perform_auth_flow(
    *,
    page: Page,
    auth_url: str,
    email: str,
    password: str,
    dev_token: str,
    proxies: Any,
    redirect_uri: str,
    signup: bool,
) -> str:
    redirect_prefix = redirect_uri.split("?")[0]
    requested_callback: dict[str, str] = {"url": ""}

    def on_request(req) -> None:
        try:
            url = req.url
            if req.is_navigation_request() and url.startswith(redirect_prefix) and "code=" in url:
                requested_callback["url"] = url
        except Exception:
            pass

    page.on("request", on_request)
    goto_with_retry(page, auth_url)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(4)
    wait_cloudflare(page, timeout_sec=90)

    if signup:
        ensure_signup_page(page)
        time.sleep(3)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        wait_cloudflare(page, timeout_sec=60)

    email_box = wait_for_email_box(page, timeout_sec=20)
    if not email_box:
        raise FlowError("email input not found")
    email_box.fill(email, timeout=5000)
    if not click_continue(page):
        raise FlowError("continue button not found after email")
    time.sleep(3)
    wait_cloudflare(page, timeout_sec=60)

    pwd_box = find_password_box(page)
    if not pwd_box:
        raise FlowError("password input not found")
    pwd_box.fill(password, timeout=5000)
    if not click_continue(page):
        raise FlowError("continue button not found after password")
    time.sleep(3)
    wait_cloudflare(page, timeout_sec=60)

    otp_code = ""
    otp_wait_rounds = 0
    for _ in range(7):
        if is_email_otp_page(page):
            otp_code = registrar.get_oai_code(dev_token, email, proxies, seen_msg_ids=set())
            if not otp_code:
                otp_wait_rounds += 1
                if otp_wait_rounds <= 2 and maybe_click_resend_email(page):
                    print(f"[warn] otp not received yet, requested resend ({otp_wait_rounds}/2)")
                    time.sleep(8)
                    continue
                raise FlowError("email OTP not received")
            if not fill_otp(page, otp_code):
                raise FlowError("otp input not found")
            click_continue(page)
            time.sleep(3)
            wait_cloudflare(page, timeout_sec=60)
            if detect_unsupported_email(page):
                raise SkipMailbox("unsupported_email")
            if detect_phone_challenge(page):
                if signup:
                    raise NeedReauth("phone verification requested")
                raise SkipMailbox("phone verification still required after reauth")
            continue
        if "localhost" in page.url and "code=" in page.url:
            break
        break

    complete_profile(page)
    time.sleep(3)
    wait_cloudflare(page, timeout_sec=60)

    callback_url = wait_for_callback(page, redirect_uri, timeout_sec=120, requested_callback=requested_callback)
    if callback_url:
        return callback_url
    if detect_phone_challenge(page):
        raise NeedReauth("phone verification requested")
    raise FlowError("callback not reached")


def launch_context(playwright, *, executable_path: str, headless: bool, artifacts_dir: str) -> tuple[Browser, BrowserContext]:
    del artifacts_dir
    browser = playwright.chromium.launch(
        executable_path=executable_path or None,
        headless=headless,
        args=[
            "--incognito",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--start-maximized",
            "--ignore-certificate-errors",
            "--allow-insecure-localhost",
            "--disable-features=CertVerifierService,ChromeRootStoreUsed",
        ],
    )
    context = browser.new_context(
        viewport={"width": 1366, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Tokyo",
        color_scheme="dark",
        ignore_https_errors=True,
    )
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en-US','en']});
        Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
        window.chrome = window.chrome || { runtime: {} };
        """
    )
    return browser, context


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


def post_configure_account(client: Sub2APIClient, *, account_id: int, platform: str, group_ids_raw: str) -> dict[str, Any]:
    account = client.get_account(account_id)
    credentials = dict(account.get("credentials") or {})
    group_ids = resolve_group_ids(client, group_ids_raw, platform)
    models = client.get_available_models(account_id)
    model_mapping = build_identity_model_mapping(models)
    if model_mapping:
        credentials["model_mapping"] = model_mapping
    updates: dict[str, Any] = {"group_ids": group_ids, "credentials": credentials}
    return client.update_account(account_id, updates)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Browser-driven tempmail registrar using Chromium + Playwright and Sub2API OAuth bridge."
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
    parser.add_argument("--max-attempts", type=int, default=3, help="Max mailbox attempts per target account")
    parser.add_argument("--retry-sleep", type=float, default=3.0, help="Sleep seconds between failed attempts")
    parser.add_argument("--sleep", type=float, default=0.0, help="Sleep seconds between accounts")
    parser.add_argument("--loop", action="store_true", help="Run continuously instead of stopping after count accounts")
    parser.add_argument("--history-file", default="", help="Optional JSONL run history file")
    parser.add_argument("--mail-sources", default="tempmail_lol,mailtm", help="Comma-separated temp mail sources")
    parser.add_argument("--duckmail-key", default="", help="Optional DuckMail API key")
    parser.add_argument("--chromium-path", default="", help="Optional Chromium executable path")
    parser.add_argument("--headless", action="store_true", help="Launch Chromium in headless mode")
    parser.add_argument("--artifacts-dir", default="artifacts", help="Directory for browser profiles and screenshots")
    parser.add_argument("--managed-accounts-file", default="managed_account_registry.jsonl", help="JSONL file used to track managed account metadata")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--telegram-bot-token", default="", help="Optional Telegram bot token for notifications")
    parser.add_argument("--telegram-chat-id", default="", help="Optional Telegram chat id; auto-detected from getUpdates if empty")
    parser.add_argument("--telegram-chat-cache-file", default="telegram_chat_id.txt", help="File used to persist resolved Telegram chat id")
    return parser


def append_history(path: str, row: dict[str, Any]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False))
        f.write("\n")


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    global DEBUG_LOGS
    DEBUG_LOGS = bool(args.debug)

    try:
        sub2api_url = normalize_base_url(args.sub2api_url)
        mail_sources = parse_mail_sources(args.mail_sources)
        if not args.admin_api_key and not args.admin_token and not args.admin_email:
            raise ValueError("provide --admin-api-key or --admin-token or --admin-email")
        if args.count <= 0 or args.max_attempts <= 0:
            raise ValueError("count/max-attempts must be > 0")
    except Exception as exc:
        print(f"[config error] {exc}")
        return 2

    admin_password = args.admin_password
    if not args.admin_api_key and not args.admin_token and args.admin_email and not admin_password:
        admin_password = getpass.getpass("Sub2API admin password: ")

    artifacts_dir = Path(args.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

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

    registrar.DUCKMAIL_KEY = args.duckmail_key or ""
    registrar.MAIL_SOURCES = {
        "tempmail_lol": "tempmail_lol" in mail_sources,
        "onesecmail": "onesecmail" in mail_sources,
        "duckmail": "duckmail" in mail_sources,
        "mailtm": "mailtm" in mail_sources,
    }

    print("[Info] browser tempmail + Sub2API registrar started")
    print(f"[Info] mail sources: {', '.join(mail_sources)}")

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
                        raise FlowError("temp mail acquisition failed")
                    print(f"[*] temp mail acquired: {email_addr}")
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
                        group_ids=[],
                        concurrency=args.concurrency,
                        priority=args.priority,
                    )
                    account_id = int(created.get("id") or 0)
                    if account_id > 0:
                        created = post_configure_account(client, account_id=account_id, platform="openai", group_ids_raw=args.group_ids)
                    print(f"[OK] account created: id={created.get('id')}, name={created.get('name')}")
                    managed_accounts.record_tempmail_success(
                        email_addr=email_addr,
                        account_id=int(created.get("id") or 0),
                    )
                    append_history(
                        args.history_file,
                        {
                            "kind": "attempt",
                            "at": time.time(),
                            "success": True,
                            "email": email_addr,
                            "email_domain": email_domain(email_addr),
                            "attempt": attempt,
                            "account": created,
                            "elapsed_sec": round(time.time() - started, 2),
                        },
                    )
                    success += 1
                    account_done = True
                    try:
                        notifier.send(
                            f"sub2api success\naccount={created.get('name')}\nemail={email_addr}\nid={created.get('id')}\nsuccess={success}"
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
                    account_last_reason = str(exc)
                    if context is not None:
                        try:
                            page = context.pages[0] if context.pages else None
                            if page is not None:
                                debug_log(f"[debug] page_url={page.url}")
                                debug_log(f"[debug] page_title={page.title()}")
                                debug_log(f"[debug] page_text={visible_text(page)[:800]}")
                        except Exception:
                            pass
                    reason = str(exc)
                    if isinstance(exc, SkipMailbox):
                        skipped_mailboxes += 1
                        print(f"[skip] attempt {attempt}: {reason}; switching to next mailbox")
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
                    notifier.send(
                        f"sub2api failed\naccount_index={idx}\nemail={email_addr or 'unknown'}\nfailed_after_attempts={args.max_attempts}\nreason={account_last_reason or 'unknown'}"
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
                        "email": email_addr,
                        "attempts_used": args.max_attempts,
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
