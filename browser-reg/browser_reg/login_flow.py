import logging
import os
import platform as _platform
import random
import shutil
import tempfile
import time
from typing import Callable, Optional
from urllib.parse import urlparse

from browserforge.fingerprints import Screen
from camoufox.sync_api import Camoufox

from browser_reg.flow import BrowserRegistrationCancelled, cleanup_stale_browser_profiles
from browser_reg.sensitive import redact_email, sanitize_text, sanitize_url_for_log

logger = logging.getLogger(__name__)


def _interruptible_sleep(seconds: float, check_cancel: Callable[[], None]) -> None:
    deadline = time.time() + max(0.0, seconds)
    while True:
        check_cancel()
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in ("0", "false", "no", "off")


def browser_login(
    email: str,
    password: str,
    proxy: str,
    wait_for_otp_fn,
    on_status_change_fn,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict:
    logger.info("[browser-reg] Login account: %s", redact_email(email))

    cf_proxy = None
    if proxy:
        pp = urlparse(proxy)
        cf_proxy = {
            "server": f"{pp.scheme}://{pp.hostname}:{pp.port}",
            "username": pp.username or "",
            "password": pp.password or "",
        }

    screenshot_dir = os.environ.get("SCREENSHOT_DIR", "/tmp/screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    cleanup_stale_browser_profiles(4 * 3600)

    tmp_profile = tempfile.mkdtemp(prefix="chatgpt_login_")
    logger.info("[browser-reg] Temp login profile: %s", tmp_profile)

    result = {
        "email": email,
        "password": password,
        "session_token": "",
        "access_token": "",
        "device_id": "",
        "csrf_token": "",
        "cookie_header": "",
        "plus_trial": False,
        "plus_trial_checked": False,
        "plus_trial_amount": 0,
        "plus_trial_currency": "",
        "plus_trial_source": "",
        "checkout_url": "",
    }

    def check_cancel() -> None:
        if should_cancel_fn and should_cancel_fn():
            raise BrowserRegistrationCancelled("browser login cancelled")

    def sleep(seconds: float) -> None:
        _interruptible_sleep(float(seconds), check_cancel)

    def safe_click(element, label: str, timeout: int = 5000) -> bool:
        try:
            element.click(timeout=timeout)
            return True
        except Exception as e:
            logger.info("[browser-reg] %s click failed, trying JS click: %s", label, sanitize_text(e))
        try:
            element.evaluate("el => el.click()")
            return True
        except Exception as e:
            logger.warning("[browser-reg] %s JS click failed: %s", label, sanitize_text(e))
            return False

    def fill_input(element, value: str) -> bool:
        try:
            element.focus()
            sleep(0.1)
            element.fill(value, timeout=5000)
            return True
        except Exception:
            pass
        try:
            element.evaluate(
                """(el, value) => {
                    el.focus();
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
                    if (setter) setter.call(el, value); else el.value = value;
                    el.dispatchEvent(new Event("input", {bubbles: true}));
                    el.dispatchEvent(new Event("change", {bubbles: true}));
                }""",
                value,
            )
            return True
        except Exception as e:
            logger.warning("[browser-reg] input fill failed: %s", sanitize_text(e))
            return False

    def find_otp_input():
        for sel in [
            'input[autocomplete="one-time-code"]:visible',
            'input[name="code"]:visible',
            'input[inputmode="numeric"]:visible',
            'input[aria-label*="code" i]:visible',
            'input[placeholder*="code" i]:visible',
            'input[type="text"]:visible',
        ]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    return el
            except Exception:
                continue
        return None

    def find_email_input():
        for sel in [
            'input[type="email"]:visible',
            'input[name="email"]:visible',
            'input[autocomplete="email"]:visible',
            'input[placeholder*="email" i]:visible',
            'input[type="text"]:visible',
        ]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    return el
            except Exception:
                continue
        return None

    def click_login_entry() -> bool:
        for sel in [
            'a[data-testid="login-button"]',
            'button[data-testid="login-button"]',
            'button:has-text("Log in")',
            'a:has-text("Log in")',
        ]:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible() and safe_click(btn, "Login"):
                    logger.info("[browser-reg] Clicked login: %s", sel)
                    return True
            except Exception:
                continue
        return False

    def wait_for_email_input(seconds: int = 60):
        for i in range(max(1, seconds)):
            email_input = find_email_input()
            if email_input:
                return email_input
            if i in (0, 10, 25, 40):
                click_login_entry()
            sleep(1)
        return None

    def click_password_flow() -> bool:
        for sel in [
            'button:has-text("Continue with password")',
            'a:has-text("Continue with password")',
            'button:has-text("continue with password")',
            'button:has-text("Use password")',
            'a:has-text("Use password")',
            'button:has-text("Password")',
            'a:has-text("Password")',
        ]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible() and safe_click(el, "Login password flow"):
                    logger.info("[browser-reg] Switched login to password flow: %s", sel)
                    return True
            except Exception:
                continue
        try:
            return bool(page.evaluate('''() => {
                const els = document.querySelectorAll('a, button, div[role="button"]');
                for (const el of els) {
                    const t = (el.textContent || '').trim().toLowerCase();
                    if ((t === 'continue with password' || t === 'use password' || t === 'password') && el.offsetParent !== null) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }'''))
        except Exception:
            return False

    def submit_login_otp() -> bool:
        logger.info("[browser-reg] Login OTP page reached")
        on_status_change_fn("WAITING_FOR_OTP")
        otp_code = wait_for_otp_fn()
        if not otp_code:
            raise RuntimeError("OTP is empty")
        otp_input = find_otp_input()
        if not otp_input or not fill_input(otp_input, otp_code):
            logger.info("[browser-reg] Login OTP input changed while filling; checking for session")
            if wait_for_session("login-otp-fill-navigation", seconds=15):
                return True
            otp_input = find_otp_input()
            if not otp_input or not fill_input(otp_input, otp_code):
                raise RuntimeError("login OTP input not found")
        for sel in ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Verify")']:
            b = page.query_selector(sel)
            if b and b.is_visible() and safe_click(b, "Login OTP continue"):
                break
        return wait_for_session("login-otp-submit", seconds=15)

    def capture_session_state(label: str) -> bool:
        try:
            session_info = page.evaluate(
                """async () => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 8000);
                    try {
                        const r = await fetch("/api/auth/session", {
                            credentials: "include",
                            signal: controller.signal
                        });
                        return await r.json();
                    } finally {
                        clearTimeout(timer);
                    }
                }"""
            )
        except Exception as e:
            logger.info("[browser-reg] Login session fetch failed at %s: %s", label, sanitize_text(e))
            return False

        if isinstance(session_info, dict) and session_info.get("accessToken"):
            result["access_token"] = session_info.get("accessToken", "")

        try:
            cookies = ctx.cookies()
        except Exception as e:
            logger.info("[browser-reg] Login cookie capture failed at %s: %s", label, sanitize_text(e))
            cookies = []
        chatgpt_cookies = [c for c in cookies if "chatgpt.com" in c.get("domain", "")]
        for c in chatgpt_cookies:
            name = c["name"]
            if name == "__Secure-next-auth.session-token":
                result["session_token"] = c["value"]
            if name in ("oai-did", "oai-device-id"):
                result["device_id"] = c["value"]
            if name == "__Host-next-auth.csrf-token":
                val = c["value"]
                result["csrf_token"] = val.split("|")[0] if "|" in val else val
        if chatgpt_cookies:
            result["cookie_header"] = "; ".join(f"{c['name']}={c['value']}" for c in chatgpt_cookies)
        logger.info(
            "[browser-reg] Login session_token=%s access_token=%s device_id=%s",
            "yes" if result["session_token"] else "no",
            "yes" if result["access_token"] else "no",
            "yes" if result["device_id"] else "no",
        )
        return bool(result["session_token"])

    def wait_for_session(label: str, seconds: int = 20) -> bool:
        for _ in range(max(1, seconds)):
            sleep(1)
            try:
                cur = page.url
            except Exception:
                return False
            if "chatgpt.com" in cur and "auth.openai.com" not in cur and capture_session_state(label):
                return True
        return False

    try:
        headless = "virtual" if _platform.system() == "Linux" else False
        geoip_enabled = _env_bool("CAMOUFOX_GEOIP", True)
        with Camoufox(
            headless=headless,
            humanize=True,
            persistent_context=True,
            user_data_dir=tmp_profile,
            screen=Screen(max_width=1920, max_height=1080),
            proxy=cf_proxy,
            geoip=geoip_enabled,
            locale="en-US",
        ) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            logger.info("[browser-reg] Opening chatgpt.com for login ...")
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            sleep(3)

            click_login_entry()
            email_input = wait_for_email_input()
            if not email_input or not fill_input(email_input, email):
                try:
                    page.screenshot(path=f"{screenshot_dir}/login_no_email.png")
                except Exception:
                    pass
                raise RuntimeError("login email input not found")
            for sel in ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Next")']:
                b = page.query_selector(sel)
                if b and b.is_visible() and safe_click(b, "Login email continue"):
                    break

            password_ready = False
            for i in range(60):
                sleep(1)
                if page.query_selector('input[type="password"]:visible') or page.query_selector('input[name="password"]:visible'):
                    password_ready = True
                    break
                if "chatgpt.com" in page.url and "auth.openai.com" not in page.url and capture_session_state("login-email-arrival"):
                    return result
                if click_password_flow():
                    sleep(2)
                    continue
                if find_otp_input():
                    if submit_login_otp():
                        return result
                    sleep(2)
                    continue
                if i in (15, 30, 45):
                    logger.info("[browser-reg] Waiting for login password field, URL: %s", sanitize_url_for_log(page.url))

            if not password_ready:
                try:
                    page.screenshot(path=f"{screenshot_dir}/login_no_password.png")
                except Exception:
                    pass
                raise RuntimeError("login password input not found")

            pwd_input = page.query_selector('input[type="password"]') or page.query_selector('input[name="password"]')
            if not pwd_input or not fill_input(pwd_input, password):
                raise RuntimeError("login password input not found")
            for sel in ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Next")']:
                b = page.query_selector(sel)
                if b and b.is_visible() and safe_click(b, "Login password continue"):
                    break

            for i in range(120):
                sleep(1)
                cur = page.url
                if "chatgpt.com" in cur and "auth.openai.com" not in cur and capture_session_state("login-arrival"):
                    return result
                if find_otp_input():
                    if submit_login_otp():
                        return result
                if i in (30, 60):
                    logger.info("[browser-reg] Login wait URL: %s", sanitize_url_for_log(cur))

            try:
                page.screenshot(path=f"{screenshot_dir}/login_no_session.png")
            except Exception:
                pass
            raise RuntimeError("login did not produce session")
    finally:
        shutil.rmtree(tmp_profile, ignore_errors=True)
        logger.info("[browser-reg] Temp login profile removed: %s", tmp_profile)
