"""
Camoufox-based ChatGPT registration flow.
Adapted from: https://github.com/DanOps-1/Gpt-Agreement-Payment/blob/feat/whatsapp-relay/CTF-reg/browser_register.py

Purpose: Run Turnstile/anti-fraud fingerprinting through a real browser to avoid
accounts being flagged by internal risk control (registration OK but Team invite
disabled).

Flow:
  1. Camoufox launch → goto https://chatgpt.com/
  2. Click Sign up → redirect to auth.openai.com
  3. Fill email → Continue
  4. Fill password → Continue (may trigger Turnstile, Camoufox passes)
  5. Receive OTP via caller-provided mail service → fill → Continue
  6. Fill name/birthday → Continue
  7. Return to chatgpt.com → fetch access_token from /api/auth/session
  8. Extract cookies: session_token / oai-did

Returns: {email, password, session_token, access_token, device_id, cookie_header, ...}
"""

import os
import random
import re
import time
import logging
import tempfile
import shutil
from typing import Any, Callable, Optional

from browser_reg.sensitive import redact_email, sanitize_text, sanitize_url_for_log

logger = logging.getLogger(__name__)

DEFAULT_STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)

_CHECKOUT_AMOUNT_KEYS = {
    "due",
    "amount_due",
    "amount_total",
    "total_amount",
    "amount_remaining",
    "total",
}
_CHECKOUT_AMOUNT_EXCLUDED_PATH_PARTS = {
    "line_items",
    "items",
    "prices",
    "price",
    "unit_amount",
    "unit_amount_decimal",
    "tax_amount",
    "discount_amount",
}


class BrowserRegistrationCancelled(RuntimeError):
    pass


def _interruptible_sleep(seconds: float, check_cancel: Callable[[], None]) -> None:
    deadline = time.time() + max(0.0, seconds)
    while True:
        check_cancel()
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _parse_checkout_amount(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    text = str(value).strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    return None


def _path_has_amount_exclusion(path: tuple[str, ...]) -> bool:
    return any(part.lower() in _CHECKOUT_AMOUNT_EXCLUDED_PATH_PARTS for part in path)


def _iter_checkout_amount_candidates(value: Any, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_path = path + (key_text,)
            if key_text.lower() in _CHECKOUT_AMOUNT_KEYS and not _path_has_amount_exclusion(child_path):
                amount = _parse_checkout_amount(child)
                if amount is not None:
                    yield ".".join(child_path), amount
            if isinstance(child, (dict, list)):
                yield from _iter_checkout_amount_candidates(child, child_path)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            if isinstance(child, (dict, list)):
                yield from _iter_checkout_amount_candidates(child, path + (str(idx),))


def _select_checkout_amount(payload: dict) -> tuple[Optional[int], str]:
    candidates = list(_iter_checkout_amount_candidates(payload))
    if not candidates:
        return None, "unknown"

    preferred_keys = (
        "due",
        "amount_due",
        "amount_total",
        "total_amount",
        "amount_remaining",
        "total",
    )
    preferred_contexts = ("total_summary", "invoice", "checkout", "session", "subscription")
    for key in preferred_keys:
        for source, amount in candidates:
            parts = tuple(source.lower().split("."))
            if parts[-1] == key and any(ctx in parts for ctx in preferred_contexts):
                return amount, source
    return candidates[0][1], candidates[0][0]


def _trial_probe_currency(info: dict) -> str:
    init_data = info.get("stripe_init") if isinstance(info, dict) else {}
    checkout_data = info.get("checkout_data") if isinstance(info, dict) else {}
    if not isinstance(init_data, dict):
        init_data = {}
    if not isinstance(checkout_data, dict):
        checkout_data = {}
    invoice = init_data.get("invoice") if isinstance(init_data.get("invoice"), dict) else {}
    total_summary = init_data.get("total_summary") if isinstance(init_data.get("total_summary"), dict) else {}
    return str(
        init_data.get("currency")
        or invoice.get("currency")
        or total_summary.get("currency")
        or checkout_data.get("currency")
        or ""
    ).upper()


def cleanup_stale_browser_profiles(max_age_seconds: float = 4 * 3600) -> int:
    """Remove old temp profiles left by killed browser processes."""
    now = time.time()
    removed = 0
    tmp_root = tempfile.gettempdir()
    try:
        names = os.listdir(tmp_root)
    except OSError:
        return 0
    for name in names:
        if not name.startswith("chatgpt_reg_"):
            continue
        path = os.path.join(tmp_root, name)
        try:
            if now - os.path.getmtime(path) < max_age_seconds:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def _gen_name() -> tuple[str, str]:
    first_names = [
        "James", "John", "Emily", "Sophia", "Michael", "Oliver", "Emma",
        "William", "Amelia", "Lucas", "Mia", "Ethan",
    ]
    last_names = [
        "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
        "Miller", "Davis", "Rodriguez", "Martinez",
    ]
    return random.choice(first_names), random.choice(last_names)


def _gen_birthday() -> tuple[str, str, str]:
    year = random.randint(1980, 2000)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return str(month).zfill(2), str(day).zfill(2), str(year)


def browser_register(
    email: str,
    password: str,
    proxy: str,
    wait_for_otp_fn,
    on_status_change_fn,
    first_name: str = "",
    last_name: str = "",
    birthday: str = "",
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict:
    """
    Register a ChatGPT account using a real browser.

    Args:
        email:         Email address for registration.
        password:      Password for the ChatGPT account.
        proxy:         Browser proxy (e.g., socks5://host:10813).
        wait_for_otp_fn: Function to block and wait for OTP string.
        on_status_change_fn: Callback to update status (e.g., "WAITING_FOR_OTP").
        first_name:    First name (random if empty).
        last_name:     Last name (random if empty).
        birthday:      Birthday as "MM/DD/YYYY" (random if empty).
        should_cancel_fn: Optional callback checked between browser actions.

    Returns:
        dict with: email, password, session_token, access_token, device_id,
                   plus_trial, checkout_url, etc.
    """
    from camoufox.sync_api import Camoufox
    from browserforge.fingerprints import Screen

    if not first_name or not last_name:
        first_name, last_name = _gen_name()
    if birthday:
        parts = birthday.split("/")
        if len(parts) == 3:
            bmonth, bday, byear = parts[0], parts[1], parts[2]
        else:
            bmonth, bday, byear = _gen_birthday()
    else:
        bmonth, bday, byear = _gen_birthday()
    logger.info(f"[browser-reg] Account: {redact_email(email)}")
    logger.info("[browser-reg] Name fields prepared")

    # Build proxy config for Camoufox.
    cf_proxy = None
    if proxy:
        from urllib.parse import urlparse
        pp = urlparse(proxy)
        cf_proxy = {
            "server": f"{pp.scheme}://{pp.hostname}:{pp.port}",
            "username": pp.username or "",
            "password": pp.password or "",
        }

    screenshot_dir = os.environ.get("SCREENSHOT_DIR", "/tmp/screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)

    removed_profiles = cleanup_stale_browser_profiles(4 * 3600)
    if removed_profiles:
        logger.info(f"[browser-reg] Removed stale temp profiles: {removed_profiles}")

    tmp_profile = tempfile.mkdtemp(prefix="chatgpt_reg_")
    logger.info(f"[browser-reg] Temp profile: {tmp_profile}")

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
            raise BrowserRegistrationCancelled("browser registration cancelled")

    def sleep(seconds: float) -> None:
        _interruptible_sleep(float(seconds), check_cancel)

    def _js_fill_input(element, value: str) -> None:
        element.evaluate(
            """(el, value) => {
                el.focus();
                const proto = el instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
                if (setter) {
                    setter.call(el, value);
                } else {
                    el.value = value;
                }
                el.dispatchEvent(new Event("input", {bubbles: true}));
                el.dispatchEvent(new Event("change", {bubbles: true}));
            }""",
            value,
        )

    def _fill_input_without_pointer(element, value: str) -> bool:
        """Fill an input without a pointer click; labels can intercept OTP clicks."""
        try:
            element.focus()
            sleep(0.1)
            element.fill(value, timeout=5000)
            return True
        except Exception as e:
            logger.info(f"[browser-reg] Direct input fill failed, trying JS fill: {sanitize_text(e)}")

        try:
            _js_fill_input(element, value)
            return True
        except Exception as e:
            logger.info(f"[browser-reg] JS input fill failed, trying keyboard fill: {sanitize_text(e)}")

        try:
            element.focus()
            sleep(0.1)
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            page.keyboard.type(value, delay=random.randint(20, 60))
            return True
        except Exception as e:
            logger.warning(f"[browser-reg] Keyboard input fill failed: {sanitize_text(e)}")
            return False

    def _safe_click(element, label: str, timeout: int = 5000) -> bool:
        try:
            element.click(timeout=timeout)
            return True
        except Exception as e:
            logger.info(f"[browser-reg] {label} click failed, trying JS click: {sanitize_text(e)}")
        try:
            element.evaluate("el => el.click()")
            return True
        except Exception as e:
            logger.warning(f"[browser-reg] {label} JS click failed: {sanitize_text(e)}")
            return False

    try:
        import platform as _platform
        _headless = "virtual" if _platform.system() == "Linux" else False

        check_cancel()
        with Camoufox(
            headless=_headless,
            humanize=True,
            persistent_context=True,
            user_data_dir=tmp_profile,
            screen=Screen(max_width=1920, max_height=1080),
            proxy=cf_proxy,
            geoip=True,
            locale="en-US",
        ) as ctx:
            check_cancel()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            def _capture_session_state(label: str) -> bool:
                try:
                    session_info = page.evaluate('''async () => {
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
                    }''')
                except Exception as e:
                    logger.info(f"[browser-reg] Session fetch failed at {label}: {sanitize_text(e)}")
                    return False

                access_token = ""
                if isinstance(session_info, dict):
                    access_token = session_info.get("accessToken", "") or ""
                if access_token:
                    result["access_token"] = access_token

                try:
                    all_cookies = ctx.cookies()
                except Exception as e:
                    logger.info(f"[browser-reg] Cookie capture failed at {label}: {sanitize_text(e)}")
                    all_cookies = []

                chatgpt_cookies = [c for c in all_cookies if "chatgpt.com" in c.get("domain", "")]
                for c in chatgpt_cookies:
                    n = c["name"]
                    if n == "__Secure-next-auth.session-token":
                        result["session_token"] = c["value"]
                    if n in ("oai-did", "oai-device-id"):
                        result["device_id"] = c["value"]
                    if n == "__Host-next-auth.csrf-token":
                        val = c["value"]
                        result["csrf_token"] = val.split("|")[0] if "|" in val else val
                if chatgpt_cookies:
                    result["cookie_header"] = "; ".join(
                        f"{c['name']}={c['value']}" for c in chatgpt_cookies
                    )

                if result["access_token"]:
                    logger.info(f"[browser-reg] access_token length: {len(result['access_token'])}")
                logger.info(
                    f"[browser-reg] session_token={'yes' if result['session_token'] else 'no'} "
                    f"device_id={'yes' if result['device_id'] else 'no'}"
                )
                return bool(result["access_token"])

            # --- [1] Open ChatGPT homepage, click "Sign up" ---
            logger.info("[browser-reg] Opening chatgpt.com ...")
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector(
                    'button[data-testid="signup-button"], a[data-testid="signup-button"]',
                    state="visible", timeout=20000,
                )
            except Exception:
                pass
            sleep(3)

            clicked_signup = False
            for sel in [
                'a[data-testid="signup-button"]',
                'button[data-testid="signup-button"]',
                'button:has-text("Sign up for free")',
                'a:has-text("Sign up for free")',
                'button:has-text("Sign up")',
                'a:has-text("Sign up")',
            ]:
                try:
                    btns = page.query_selector_all(sel)
                except Exception:
                    continue
                for btn in btns:
                    try:
                        if not btn.is_visible():
                            continue
                        text = btn.inner_text().lower()
                        if "sign up" not in text:
                            continue
                        try:
                            btn.click(timeout=5000)
                        except Exception:
                            btn.evaluate("el => el.click()")
                        clicked_signup = True
                        logger.info(f"[browser-reg] Clicked Sign up ({sel}): {text[:40]}")
                        break
                    except Exception as e:
                        if "attached to the DOM" in str(e) or "detached" in str(e).lower():
                            continue
                        logger.warning(f"[browser-reg] Click error: {sanitize_text(e)}")
                if clicked_signup:
                    break

            if not clicked_signup:
                page.screenshot(path=f"{screenshot_dir}/no_signup.png")
                raise RuntimeError(f"Sign up button not found, URL={sanitize_url_for_log(page.url)}")

            # Wait for redirect to auth.openai.com
            pre_url = page.url
            for i in range(20):
                sleep(1)
                if "auth.openai.com" in page.url or page.query_selector('input[type="email"]'):
                    break
                if i == 5 and page.url == pre_url:
                    logger.info("[browser-reg] Sign up click had no effect, retrying")
                    try:
                        btn = page.query_selector(
                            'button[data-testid="signup-button"], a[data-testid="signup-button"]'
                        )
                        if btn:
                            btn.click(timeout=3000)
                    except Exception:
                        pass
            logger.info(f"[browser-reg] URL: {sanitize_url_for_log(page.url)}")

            # --- [2] Fill email ---
            logger.info("[browser-reg] Filling email ...")
            page.wait_for_selector('input[type="email"], input[name="email"]', timeout=30000)
            for _try in range(4):
                try:
                    ei = (page.query_selector('input[type="email"]')
                          or page.query_selector('input[name="email"]'))
                    if not ei:
                        sleep(0.5)
                        continue
                    ei.click(timeout=5000)
                    sleep(0.3)
                    ei2 = (page.query_selector('input[type="email"]')
                           or page.query_selector('input[name="email"]'))
                    (ei2 or ei).fill(email)
                    break
                except Exception as e:
                    if "not attached" in str(e).lower() or "detached" in str(e).lower():
                        logger.info(f"[browser-reg] Email input detached, retry {_try+1}/4")
                        sleep(0.5)
                        continue
                    raise
            sleep(random.uniform(0.5, 1.2))

            for sel in ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Next")']:
                b = page.query_selector(sel)
                if b and b.is_visible():
                    b.click()
                    logger.info(f"[browser-reg] Email continue: {sel}")
                    break
            sleep(3)

            # --- [3] Email-verification page → switch to password flow ---
            # 2026 flow: after email submit on chatgpt.com, it redirects to
            # auth.openai.com/email-verification with OTP input + "Continue with password" button.
            # We click "Continue with password" to skip first OTP.
            logger.info("[browser-reg] Waiting for auth.openai.com redirect ...")
            for wait_i in range(40):
                sleep(1)
                cur = page.url
                if "auth.openai.com" in cur:
                    logger.info(f"[browser-reg] Reached auth page: {sanitize_url_for_log(cur)}")
                    break
                if wait_i == 20:
                    page.screenshot(path=f"{screenshot_dir}/wait20.png")
                    logger.info(f"[browser-reg] 20s still waiting, URL: {sanitize_url_for_log(cur, 80)}")

            # Wait for OTP input or password link to appear
            sleep(2)
            logger.info(f"[browser-reg] Post-email URL: {sanitize_url_for_log(page.url)}")
            page.screenshot(path=f"{screenshot_dir}/before_password_switch.png")

            # Click "Continue with password" button to skip first OTP
            if not page.query_selector('input[type="password"]:visible'):
                switched = False
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
                        if el and el.is_visible():
                            el.click(timeout=5000)
                            switched = True
                            logger.info(f"[browser-reg] Switched to password flow: {sel}")
                            break
                    except Exception:
                        continue

                if not switched:
                    # JS fallback — look for button/link with exact "Continue with password"
                    try:
                        found = page.evaluate('''() => {
                            const els = document.querySelectorAll('a, button, div[role="button"]');
                            for (const el of els) {
                                const t = (el.textContent || '').trim();
                                if (t === 'Continue with password' && el.offsetParent !== null) {
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }''')
                        if found:
                            logger.info("[browser-reg] Switched to password flow via JS")
                            switched = True
                    except Exception:
                        pass

                if not switched:
                    page.screenshot(path=f"{screenshot_dir}/no_password_link.png")
                    logger.warning("[browser-reg] Could not find 'Continue with password' button")

                sleep(3)

            # --- [4] Set password ---
            logger.info("[browser-reg] Waiting for password field ...")
            try:
                page.wait_for_selector(
                    'input[type="password"], input[name="password"]',
                    state="visible", timeout=30000,
                )
                pwd_input = (page.query_selector('input[type="password"]:visible')
                             or page.query_selector('input[name="password"]:visible'))
                pwd_input.click()
                sleep(0.3)
                pwd_input.fill(password)
                sleep(random.uniform(0.5, 1.2))
                for sel in [
                    'button[type="submit"]', 'button:has-text("Continue")',
                    'button:has-text("Create")', 'button:has-text("Next")',
                ]:
                    b = page.query_selector(sel)
                    if b and b.is_visible():
                        b.click()
                        logger.info(f"[browser-reg] Password continue: {sel}")
                        break
                logger.info("[browser-reg] Password set successfully")
            except Exception as e:
                logger.warning(f"[browser-reg] Password field not found: {sanitize_text(e)}")
                page.screenshot(path=f"{screenshot_dir}/no_password.png")

            sleep(3)
            logger.info(f"[browser-reg] Post-password URL: {sanitize_url_for_log(page.url)}")

            # --- [5] Second OTP (after password, for email verification) ---
            def _is_email_code_page() -> bool:
                if "auth.openai.com/email-verification" not in page.url:
                    return False
                try:
                    body_text = page.locator("body").inner_text(timeout=1000).lower()
                except Exception:
                    body_text = ""
                return (
                    "check your inbox" in body_text
                    or "verification code" in body_text
                    or "enter the verification code" in body_text
                )

            def _find_otp_input():
                for sel in [
                    'input[autocomplete="one-time-code"]:visible',
                    'input[name="code"]:visible',
                    'input[inputmode="numeric"]:visible',
                    'input[aria-label*="code" i]:visible',
                    'input[placeholder*="code" i]:visible',
                    'input[type="text"]:visible',
                    'input:not([type="hidden"]):not([type="password"]):visible',
                ]:
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            return el
                    except Exception:
                        continue
                return None

            # Wait for OTP page to appear
            for wait_i in range(30):
                sleep(1)
                try:
                    if _find_otp_input() or _is_email_code_page():
                        logger.info("[browser-reg] Second OTP page reached")
                        break
                    if "chatgpt.com" in page.url and "auth.openai.com" not in page.url:
                        logger.info("[browser-reg] Already at chatgpt.com, skipping OTP")
                        break
                except Exception as e:
                    if "Execution context was destroyed" in str(e):
                        continue
                    logger.warning(f"[browser-reg] OTP poll error: {sanitize_text(e)}")
                if wait_i == 15:
                    page.screenshot(path=f"{screenshot_dir}/wait_otp2.png")

            if _find_otp_input() or _is_email_code_page():
                logger.info("[browser-reg] Waiting for second OTP ...")
                on_status_change_fn("WAITING_FOR_OTP")
                otp_code = None

                # The browser service must not own OTP timeout policy. The
                # orchestrator waits for OTP and cancels this flow when a job
                # expires or is cleaned up.
                otp_code = wait_for_otp_fn()
                if not otp_code:
                    raise RuntimeError("OTP is empty")
                logger.info("[browser-reg] Got OTP")

                otp_filled = False
                single = _find_otp_input()
                single_maxlength = ""
                if single:
                    try:
                        single_maxlength = (single.get_attribute("maxlength") or "").strip()
                    except Exception:
                        single_maxlength = ""

                if single and single_maxlength != "1":
                    otp_filled = _fill_input_without_pointer(single, otp_code)
                else:
                    digits = []
                    for sel in [
                        'input[maxlength="1"][inputmode="numeric"]',
                        'input[maxlength="1"]',
                    ]:
                        try:
                            digits = [el for el in page.query_selector_all(sel) if el.is_visible()]
                        except Exception:
                            digits = []
                        if len(digits) >= 6:
                            break

                    if len(digits) >= 6:
                        for i, ch in enumerate(otp_code[:6]):
                            if not _fill_input_without_pointer(digits[i], ch):
                                break
                            sleep(0.1)
                        else:
                            otp_filled = True

                if single and not otp_filled:
                    logger.info("[browser-reg] Split OTP fill did not work, retrying as a single input")
                    otp_filled = _fill_input_without_pointer(single, otp_code)

                if otp_filled:
                    logger.info("[browser-reg] OTP input filled")
                else:
                    page.screenshot(path=f"{screenshot_dir}/otp2_fail.png")
                    raise RuntimeError("Second OTP input not found")

                sleep(0.8)
                for sel in [
                    'button[type="submit"]', 'button:has-text("Continue")',
                    'button:has-text("Verify")', 'button:has-text("Next")',
                ]:
                    b = page.query_selector(sel)
                    if b and b.is_visible():
                        if _safe_click(b, "OTP continue"):
                            logger.info(f"[browser-reg] OTP continue: {sel}")
                            break
                sleep(4)

            sleep(3)

            # --- [6] /about-you: Full name + Birthday ---
            logger.info(f"[browser-reg] Post-OTP URL: {sanitize_url_for_log(page.url)}")
            sleep(5)

            for _ in range(20):
                sleep(1)
                if "about-you" in page.url or "chatgpt.com" in page.url:
                    break

            def _enum_inputs():
                try:
                    return page.evaluate('''() => {
                        return Array.from(document.querySelectorAll('input')).map((el, idx) => {
                            const r = el.getBoundingClientRect();
                            const cs = getComputedStyle(el);
                            return {
                                idx,
                                type: (el.type || '').toLowerCase(),
                                name: el.name || '',
                                placeholder: el.placeholder || '',
                                ariaLabel: el.getAttribute('aria-label') || '',
                                label: (el.labels && el.labels[0] && el.labels[0].innerText) || '',
                                value: el.value || '',
                                visible: (r.width > 0 && r.height > 0 &&
                                          cs.visibility !== 'hidden' && cs.display !== 'none'),
                            };
                        });
                    }''') or []
                except Exception:
                    return []

            def _is_birthday(meta: dict) -> bool:
                blob = " ".join([
                    meta.get("type", ""), meta.get("name", ""),
                    meta.get("placeholder", ""), meta.get("ariaLabel", ""),
                    meta.get("label", ""),
                ]).lower()
                if meta.get("type") == "date":
                    return True
                return any(kw in blob for kw in ("birth", "birthday", "dob", "mm/dd/yyyy"))

            full_name_input = None
            birthday_input = None
            birthday_meta = None

            for attempt in range(30):
                metas = _enum_inputs()
                visible_metas = [
                    m for m in metas if m["visible"]
                    and m["type"] not in ("hidden", "submit", "button", "checkbox", "radio", "password")
                ]
                bd = next((m for m in visible_metas if _is_birthday(m)), None)
                name_m = next((m for m in visible_metas if m is not bd and not _is_birthday(m)), None)

                if bd and name_m:
                    all_inputs_el = page.query_selector_all("input")
                    full_name_input = all_inputs_el[name_m["idx"]]
                    birthday_input = all_inputs_el[bd["idx"]]
                    birthday_meta = bd
                    logger.info(
                        f"[browser-reg] Form: name.idx={name_m['idx']} "
                        f"birthday.idx={bd['idx']} type={bd['type']}"
                    )
                    break

                if not bd and len(visible_metas) >= 2:
                    all_inputs_el = page.query_selector_all("input")
                    full_name_input = all_inputs_el[visible_metas[0]["idx"]]
                    birthday_input = all_inputs_el[visible_metas[1]["idx"]]
                    birthday_meta = visible_metas[1]
                    logger.info(f"[browser-reg] Form (legacy age): {len(visible_metas)} inputs")
                    break

                if "chatgpt.com" in page.url and "auth" not in page.url:
                    break
                if attempt == 5:
                    page.screenshot(path=f"{screenshot_dir}/about_you_wait.png")
                sleep(1)

            if full_name_input and birthday_input:
                import datetime as _dt

                full_name = f"{first_name} {last_name}"
                year = _dt.datetime.now().year - random.randint(26, 40)
                mm, dd = "01", "15"
                bd_type = (birthday_meta or {}).get("type", "")
                birthday_str = f"{year}-{mm}-{dd}" if bd_type == "date" else f"{mm}/{dd}/{year}"
                legacy_age = str(random.randint(26, 40))

                logger.info("[browser-reg] About-you fields prepared")
                try:
                    full_name_input.focus()
                    sleep(0.3)
                    page.keyboard.type(full_name, delay=random.randint(30, 80))
                    sleep(random.uniform(0.4, 0.9))

                    birthday_input.focus()
                    sleep(0.3)
                    try:
                        page.keyboard.press("Control+A")
                        page.keyboard.press("Delete")
                    except Exception:
                        pass

                    if bd_type == "date":
                        try:
                            birthday_input.fill(birthday_str)
                        except Exception:
                            page.keyboard.type(birthday_str, delay=random.randint(30, 70))
                    else:
                        if _is_birthday(birthday_meta or {}):
                            page.keyboard.type(birthday_str, delay=random.randint(30, 70))
                        else:
                            page.keyboard.type(legacy_age, delay=random.randint(40, 100))

                    sleep(random.uniform(0.4, 0.9))
                    for sel in [
                        'button:has-text("Finish")', 'button:has-text("Create")',
                        'button:has-text("Agree")', 'button[type="submit"]',
                        'button:has-text("Continue")',
                    ]:
                        b = page.query_selector(sel)
                        if b and b.is_visible():
                            b.click()
                            logger.info(f"[browser-reg] About-you continue: {sel}")
                            break
                except Exception as e:
                    logger.warning(f"[browser-reg] About-you fill error: {sanitize_text(e)}")
                    page.screenshot(path=f"{screenshot_dir}/name_fail.png")
            else:
                page.screenshot(path=f"{screenshot_dir}/no_name_form.png")
                logger.warning(f"[browser-reg] No about-you form found, URL={sanitize_url_for_log(page.url)}")

            # --- [7] Wait for redirect back to chatgpt.com ---
            logger.info("[browser-reg] Waiting for redirect to chatgpt.com ...")
            arrived = False
            last_url = ""
            for i in range(120):
                sleep(1)
                cur = page.url
                if cur != last_url:
                    logger.info(f"[browser-reg] URL@{i}s: {sanitize_url_for_log(cur)}")
                    last_url = cur

                if "chatgpt.com" in cur and "auth.openai.com" not in cur:
                    if _capture_session_state("arrival"):
                        arrived = True
                        break

                if "auth.openai.com" in cur and i % 5 == 0:
                    try:
                        body_text = page.locator("body").inner_text(timeout=1000)
                    except Exception:
                        body_text = ""
                    if "user_already_exists" in body_text:
                        page.screenshot(path=f"{screenshot_dir}/user_already_exists.png")
                        raise RuntimeError("account already exists")

                if "auth.openai.com" in cur and i % 10 == 5:
                    for sel in ['button:has-text("Continue")', 'button:has-text("Next")',
                                'button[type="submit"]']:
                        try:
                            b = page.query_selector(sel)
                            if b and b.is_visible():
                                b.click()
                                logger.info(f"[browser-reg] Intermediate click: {sel}")
                                break
                        except Exception:
                            pass

            if not arrived:
                try:
                    body_text = page.locator("body").inner_text(timeout=3000)
                except Exception:
                    body_text = ""

                if "user_already_exists" in body_text:
                    page.screenshot(path=f"{screenshot_dir}/user_already_exists.png")
                    raise RuntimeError("account already exists")

            if not arrived:
                try:
                    page.screenshot(path=f"{screenshot_dir}/no_chatgpt.png")
                except Exception:
                    pass
                raise RuntimeError(f"Did not redirect to chatgpt.com, current={sanitize_url_for_log(page.url)}")

            # --- [8] Refresh credentials if arrival capture was partial ---
            if not result["access_token"] or not result["session_token"]:
                sleep(2)
                logger.info("[browser-reg] Refreshing /api/auth/session ...")
                _capture_session_state("refresh")

            # --- [10] Optional Plus Trial Eligibility Check ---
            if result["access_token"] and _env_bool("BROWSER_CHECK_PLUS_TRIAL", False):
                try:
                    stripe_pk = (os.environ.get("STRIPE_PUBLISHABLE_KEY") or DEFAULT_STRIPE_PK).strip()
                    trial_info = page.evaluate('''async ({token, stripePk, deviceId}) => {
                        try {
                            const resp = await fetch('/backend-api/payments/checkout', {
                                method: 'POST',
                                credentials: 'include',
                                headers: {
                                    'Authorization': 'Bearer ' + token,
                                    'Content-Type': 'application/json',
                                    'oai-device-id': deviceId || '',
                                    'oai-language': 'en-US',
                                    'x-openai-target-path': '/backend-api/payments/checkout',
                                    'x-openai-target-route': '/backend-api/payments/checkout'
                                },
                                body: JSON.stringify({
                                    entry_point: 'all_plans_pricing_modal',
                                    plan_name: 'chatgptplusplan',
                                    billing_details: { country: 'ID', currency: 'IDR' },
                                    promo_campaign: {
                                        promo_campaign_id: 'plus-1-month-free',
                                        is_coupon_from_query_param: false
                                    },
                                    checkout_ui_mode: 'hosted',
                                    cancel_url: 'https://chatgpt.com/#pricing'
                                })
                            });
                            const data = await resp.json().catch(() => ({}));
                            const rawUrl = data?.url || data?.stripe_hosted_url || data?.checkout_url || '';
                            let checkoutSessionId = data?.checkout_session_id || data?.session_id || data?.id || '';
                            if (!checkoutSessionId && rawUrl) {
                                const match = String(rawUrl).match(/(cs_(?:live|test)_[A-Za-z0-9]+)/);
                                checkoutSessionId = match ? match[1] : '';
                            }
                            const processorEntity = data?.processor_entity || data?.processor || 'openai_llc';
                            const checkoutUrl = rawUrl || (checkoutSessionId ? `https://chatgpt.com/checkout/${processorEntity}/${checkoutSessionId}` : '');
                            let stripeStatus = 0;
                            let stripeInit = {};
                            let stripeError = '';
                            if (checkoutSessionId) {
                                const stripeJsId = (globalThis.crypto && crypto.randomUUID)
                                    ? crypto.randomUUID()
                                    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
                                const body = new URLSearchParams({
                                    browser_locale: 'en-US',
                                    browser_timezone: 'Asia/Shanghai',
                                    'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1',
                                    'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1',
                                    'elements_session_client[elements_init_source]': 'custom_checkout',
                                    'elements_session_client[referrer_host]': 'chatgpt.com',
                                    'elements_session_client[stripe_js_id]': stripeJsId,
                                    'elements_session_client[locale]': 'en',
                                    'elements_session_client[is_aggregation_expected]': 'false',
                                    'elements_options_client[stripe_js_locale]': 'auto',
                                    key: stripePk
                                });
                                const initResp = await fetch(`https://api.stripe.com/v1/payment_pages/${checkoutSessionId}/init`, {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                                    body
                                });
                                stripeStatus = initResp.status;
                                stripeInit = await initResp.json().catch(() => ({}));
                                if (!initResp.ok) {
                                    stripeError = JSON.stringify(stripeInit).slice(0, 300);
                                }
                            }
                            return {
                                status: resp.status,
                                url: checkoutUrl,
                                checkout_session_id: checkoutSessionId,
                                processor_entity: processorEntity,
                                checkout_data: {
                                    currency: data?.currency || data?.billing_details?.currency || '',
                                    amount_due: data?.amount_due,
                                    amount_total: data?.amount_total,
                                    total_amount: data?.total_amount,
                                    total: data?.total
                                },
                                stripe_status: stripeStatus,
                                stripe_error: stripeError,
                                stripe_init: {
                                    currency: stripeInit?.currency || stripeInit?.invoice?.currency || '',
                                    total_summary: stripeInit?.total_summary || null,
                                    invoice: stripeInit?.invoice || null,
                                    checkout_session: stripeInit?.checkout_session || null,
                                    subscription: stripeInit?.subscription || null,
                                    amount_due: stripeInit?.amount_due,
                                    amount_total: stripeInit?.amount_total,
                                    total_amount: stripeInit?.total_amount,
                                    amount_remaining: stripeInit?.amount_remaining,
                                    total: stripeInit?.total
                                }
                            };
                        } catch(e) { return { status: -1, url: null, error: String(e && e.message || e) }; }
                    }''', {
                        "token": result["access_token"],
                        "stripePk": stripe_pk,
                        "deviceId": result["device_id"],
                    })
                    checkout_url = trial_info.get("url", "") or ""
                    result["checkout_url"] = checkout_url
                    amount, source = _select_checkout_amount(trial_info.get("stripe_init") or {})
                    if amount is None:
                        checkout_amount, checkout_source = _select_checkout_amount(trial_info.get("checkout_data") or {})
                        if checkout_amount is not None:
                            amount, source = checkout_amount, f"checkout_data.{checkout_source}"
                    if amount is None:
                        probe_error = sanitize_text(
                            trial_info.get("error") or trial_info.get("stripe_error") or ""
                        )
                        probe_message = (
                            "[browser-reg] Plus trial probe did not expose amount "
                            f"(checkout_status={trial_info.get('status')}, "
                            f"stripe_status={trial_info.get('stripe_status')}, "
                            f"url={'yes' if checkout_url else 'no'}, "
                            f"error={probe_error})"
                        )
                        if (
                            trial_info.get("stripe_status") == 403
                            and "invalid_request_http_origin" in probe_error
                            and checkout_url
                        ):
                            logger.info(
                                probe_message
                                + "; Stripe init is blocked by browser Origin, "
                                + "GoPay payment flow will validate amount"
                            )
                        else:
                            logger.warning(probe_message)
                    else:
                        result["plus_trial_checked"] = True
                        result["plus_trial_amount"] = amount
                        result["plus_trial_currency"] = _trial_probe_currency(trial_info)
                        result["plus_trial_source"] = source
                        result["plus_trial"] = amount == 0
                        logger.info(
                            f"[browser-reg] Plus trial eligible={result['plus_trial']} "
                            f"amount={amount} {result['plus_trial_currency'] or '?'} "
                            f"source={source} url={'yes' if checkout_url else 'no'}"
                        )
                except Exception as e:
                    logger.warning(f"[browser-reg] Plus trial check failed: {sanitize_text(e)}")
            else:
                logger.info(
                    "[browser-reg] Plus trial checkout probe disabled; "
                    "account eligibility remains unknown until payment validation"
                )

            # --- [11] Validation ---
            # Account creation is complete once a session cookie exists.
            # access_token is derived from that cookie and can be refreshed by
            # the dashboard later, so it must not make registration fail.
            if not result["session_token"]:
                try:
                    page.screenshot(path=f"{screenshot_dir}/missing_token.png")
                except Exception:
                    pass
                raise RuntimeError(
                    f"Missing credentials: access_token={bool(result['access_token'])} "
                    f"session_token={bool(result['session_token'])}"
                )
            if not result["access_token"]:
                logger.warning("[browser-reg] access_token missing; account will be registered with session_token only")
    finally:
        try:
            shutil.rmtree(tmp_profile, ignore_errors=True)
            logger.info(f"[browser-reg] Temp profile removed: {tmp_profile}")
        except Exception:
            pass

    return result
