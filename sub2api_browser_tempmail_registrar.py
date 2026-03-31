#!/usr/bin/env python3
import argparse
import getpass
import json
import random
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

import registrar_core as registrar
from sub2api_tempmail_registrar import Sub2APIClient, normalize_base_url, parse_group_ids, parse_mail_sources


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
    ]:
        if maybe_fill(loc, otp_code):
            return True
    return False


def complete_profile(page: Page) -> None:
    first, last, year, month, day = random_identity()
    birthday = f"{year}-{month}-{day}"
    age_value = str(random.randint(22, 34))
    text = visible_text(page)
    if not any(token in text.lower() for token in ["about", "tell us", "名字", "姓", "birth", "生日", "about you", "年龄", "age"]):
        return

    try:
        print("[debug] profile inputs=", page.locator("input").evaluate_all(
            "els => els.map(e => ({type:e.type, name:e.name, placeholder:e.placeholder, aria:e.getAttribute('aria-label'), value:e.value}))"
        ))
        print("[debug] profile widgets=", page.locator("input, button, select, [role='combobox'], [role='spinbutton']").evaluate_all(
            "els => els.map(e => ({tag:e.tagName, type:e.getAttribute('type'), name:e.getAttribute('name'), text:(e.innerText||e.textContent||'').trim().slice(0,80), aria:e.getAttribute('aria-label'), value:e.value||''}))"
        ))
        form = page.locator("form")
        if form.count() > 0:
            print("[debug] profile form html=", form.first.evaluate("el => el.outerHTML.slice(0, 5000)"))
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
            print(f"[debug] birthday selects set to {birthday}")
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
            print(f"[debug] birthday set to {birthday}")
            try:
                print(f"[debug] birthday current hidden={birthday_input.input_value(timeout=1000)}")
            except Exception:
                pass
    except Exception:
        pass

    try:
        age_input = page.locator("input[name='age']")
        if age_input.count() > 0:
            print(f"[debug] age current={age_input.input_value(timeout=1000)}")
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


def is_email_otp_page(page: Page) -> bool:
    text = visible_text(page).lower()
    url = page.url.lower()
    return (
        "email-verification" in url
        or any(
            token in text
            for token in [
                "验证码",
                "code",
                "check your inbox",
                "检查您的收件箱",
                "输入我们刚刚向",
                "email verification",
            ]
        )
    )


def wait_for_callback(page: Page, redirect_uri: str, timeout_sec: int = 90) -> Optional[str]:
    deadline = time.time() + timeout_sec
    redirect_prefix = redirect_uri.split("?")[0]
    while time.time() < deadline:
        current_url = page.url
        if current_url.startswith(redirect_prefix) and "code=" in current_url:
            return current_url
        if "localhost" in current_url and "code=" in current_url:
            return current_url
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
    page.goto(auth_url, wait_until="domcontentloaded", timeout=120000)
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
    for _ in range(5):
        if is_email_otp_page(page):
            otp_code = registrar.get_oai_code(dev_token, email, proxies, seen_msg_ids=set())
            if not otp_code:
                raise FlowError("email OTP not received")
            if not fill_otp(page, otp_code):
                raise FlowError("otp input not found")
            click_continue(page)
            time.sleep(3)
            wait_cloudflare(page, timeout_sec=60)
            if detect_phone_challenge(page):
                if signup:
                    raise NeedReauth("phone verification requested")
                raise FlowError("phone verification still required after reauth")
            continue
        if "localhost" in page.url and "code=" in page.url:
            break
        break

    complete_profile(page)
    time.sleep(3)
    wait_cloudflare(page, timeout_sec=60)

    callback_url = wait_for_callback(page, redirect_uri, timeout_sec=120)
    if callback_url:
        return callback_url
    if detect_phone_challenge(page):
        raise NeedReauth("phone verification requested")
    raise FlowError("callback not reached")


def launch_context(playwright, *, executable_path: str, headless: bool, artifacts_dir: str) -> tuple[Browser, BrowserContext]:
    tempfile.mkdtemp(prefix="pw-openai-", dir=artifacts_dir)
    browser = playwright.chromium.launch(
        executable_path=executable_path or None,
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--start-maximized",
        ],
    )
    context = browser.new_context(
        viewport={"width": 1366, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Tokyo",
        color_scheme="dark",
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
    parser.add_argument("--group-ids", default="2", help="Account group IDs, comma-separated")
    parser.add_argument("--concurrency", type=int, default=10, help="Account concurrency")
    parser.add_argument("--priority", type=int, default=1, help="Account priority")
    parser.add_argument("--count", type=int, default=1, help="How many accounts to register this run")
    parser.add_argument("--max-attempts", type=int, default=3, help="Max mailbox attempts per target account")
    parser.add_argument("--retry-sleep", type=float, default=3.0, help="Sleep seconds between failed attempts")
    parser.add_argument("--sleep", type=float, default=2.0, help="Sleep seconds between accounts")
    parser.add_argument("--history-file", default="", help="Optional JSONL run history file")
    parser.add_argument("--mail-sources", default="tempmail_lol,mailtm", help="Comma-separated temp mail sources")
    parser.add_argument("--duckmail-key", default="", help="Optional DuckMail API key")
    parser.add_argument("--chromium-path", default="", help="Optional Chromium executable path")
    parser.add_argument("--headless", action="store_true", help="Launch Chromium in headless mode")
    parser.add_argument("--artifacts-dir", default="artifacts", help="Directory for browser profiles and screenshots")
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

    try:
        sub2api_url = normalize_base_url(args.sub2api_url)
        group_ids = parse_group_ids(args.group_ids)
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
    with sync_playwright() as pw:
        for idx in range(1, args.count + 1):
            print(f"\n========== account {idx}/{args.count} ==========")
            account_done = False
            for attempt in range(1, args.max_attempts + 1):
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

                    created = client.create_from_oauth(
                        session_id=session_id_final,
                        code=registrar._parse_callback_url(callback_url).get("code", ""),
                        state=registrar._parse_callback_url(callback_url).get("state", ""),
                        redirect_uri=args.redirect_uri,
                        proxy_id=args.sub2api_proxy_id,
                        name=email_addr,
                        group_ids=group_ids,
                        concurrency=args.concurrency,
                        priority=args.priority,
                    )
                    print(f"[OK] account created: id={created.get('id')}, name={created.get('name')}")
                    append_history(
                        args.history_file,
                        {
                            "at": time.time(),
                            "success": True,
                            "email": email_addr,
                            "attempt": attempt,
                            "account": created,
                            "elapsed_sec": round(time.time() - started, 2),
                        },
                    )
                    context.close()
                    success += 1
                    account_done = True
                    break
                except Exception as exc:
                    if context is not None:
                        try:
                            page = context.pages[0] if context.pages else None
                            if page is not None:
                                print(f"[debug] page_url={page.url}")
                                print(f"[debug] page_title={page.title()}")
                                print(f"[debug] page_text={visible_text(page)[:800]}")
                        except Exception:
                            pass
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
            if idx < args.count and args.sleep > 0:
                time.sleep(args.sleep)

    print(f"\nDone: success {success}/{args.count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
