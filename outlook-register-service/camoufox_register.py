"""
Camoufox-based Outlook account registration flow.
Ported from: https://github.com/LainsNL/OutlookRegister

Usage:
    python -m browser_reg.outlook_flow --proxy socks5://127.0.0.1:10814
"""

from __future__ import annotations

import base64
import hashlib
import logging
import math
import os
import random
import re
import secrets
import shutil
import string
import tempfile
import time
from typing import Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

logger = logging.getLogger(__name__)


OUTLOOK_CONSENT_SELECTORS = [
    'button:has-text("Agree and continue")',
    'button:has-text("Accept and continue")',
    'button:text-is("Agree")',
    'button:text-is("Accept")',
    'button:has-text("\u540c\u610f\u5e76\u7ee7\u7eed")',
    'button:has-text("\u63a5\u53d7\u5e76\u7ee7\u7eed")',
    'button:text-is("\u540c\u610f")',
    '[role="button"]:has-text("Agree and continue")',
    '[role="button"]:has-text("Accept and continue")',
    '[role="button"]:has-text("\u540c\u610f\u5e76\u7ee7\u7eed")',
    '[role="button"]:has-text("\u63a5\u53d7\u5e76\u7ee7\u7eed")',
    'text="Agree and continue"',
    'text="Accept and continue"',
    'text="\u540c\u610f\u5e76\u7ee7\u7eed"',
    'text="\u63a5\u53d7\u5e76\u7ee7\u7eed"',
    'input[type="submit"][value*="Agree"]',
    'input[type="submit"][value*="Accept"]',
    'input[type="submit"][value*="\u540c\u610f"]',
]

OUTLOOK_EMAIL_INPUT_SELECTORS = [
    'input[aria-label*="New email"]',
    'input[aria-label*="Create email"]',
    'input[aria-label*="email"]',
    'input[aria-label*="\u65b0\u5efa"]',
    'input[aria-label*="\u7535\u5b50\u90ae\u4ef6"]',
    'input[placeholder*="New email"]',
    'input[placeholder*="email"]',
    'input[placeholder*="\u65b0\u5efa"]',
    'input[placeholder*="\u7535\u5b50\u90ae\u4ef6"]',
    'input[name="MemberName"]',
    'input[id="MemberName"]',
    'input[id*="username" i]',
    '#usernameInput',
    'input[type="email"]',
    'input[autocomplete="username"]',
    'input:not([type="hidden"]):not([type="password"])',
]


def _is_target_closed(exc: Exception) -> bool:
    """Check if an exception is a Playwright TargetClosedError by class name."""
    return type(exc).__name__ == "TargetClosedError"


def _page_is_closed(page) -> bool:
    try:
        return bool(page.is_closed())
    except Exception as exc:
        logger.debug("[browser] page.is_closed() failed; treating page as still active: %s", exc)
        return False


def _safe_page_url(page) -> str:
    try:
        return str(page.url)
    except Exception:
        return ""

try:
    BOT_PROTECTION_WAIT = max(0, int(os.environ.get("OUTLOOK_REGISTER_BOT_PROTECTION_WAIT", "0")))
except ValueError:
    BOT_PROTECTION_WAIT = 0


# ---------------------------------------------------------------------------
# Data generators
# ---------------------------------------------------------------------------

def _gen_email_local() -> str:
    try:
        from faker import Faker
        fake = Faker("en_US")
        first = re.sub(r"[^a-z0-9]", "", fake.first_name().lower())
        last = re.sub(r"[^a-z0-9]", "", fake.last_name().lower())
        suffix = str(random.randint(10, 9999))
        candidates = [f"{first}{last}{suffix}", f"{first[0]}{last}{suffix}", f"{first}{last[0]}{suffix}"]
        return random.choice(candidates)[:24]
    except ImportError:
        chars = string.ascii_lowercase
        return "".join(random.choice(chars) for _ in range(random.randint(10, 14)))


def _gen_password() -> str:
    upper = random.choice(string.ascii_uppercase)
    lower = "".join(random.choice(string.ascii_lowercase) for _ in range(8))
    digits = "".join(random.choice(string.digits) for _ in range(3))
    special = random.choice("!@#$%&")
    pwd = list(upper + lower + digits + special)
    random.shuffle(pwd)
    return "".join(pwd)


def _gen_name() -> tuple[str, str]:
    try:
        from faker import Faker
        fake = Faker("en_US")
        return fake.first_name(), fake.last_name()
    except ImportError:
        firsts = ["James", "Emily", "Michael", "Sophia", "Oliver", "Emma"]
        lasts = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia"]
        return random.choice(firsts), random.choice(lasts)


# ---------------------------------------------------------------------------
# Bezier mouse helpers
# ---------------------------------------------------------------------------

def _bezier_move(page, x1, y1, x2, y2):
    dist = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
    steps = max(12, min(40, int(dist / 8)))
    cx1 = x1 + (x2 - x1) * random.uniform(0.15, 0.4) + random.randint(-20, 20)
    cy1 = y1 + (y2 - y1) * random.uniform(0.0, 0.3) + random.randint(-20, 20)
    cx2 = x1 + (x2 - x1) * random.uniform(0.6, 0.85) + random.randint(-10, 10)
    cy2 = y1 + (y2 - y1) * random.uniform(0.7, 1.0) + random.randint(-10, 10)
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        px = u**3 * x1 + 3 * u**2 * t * cx1 + 3 * u * t**2 * cx2 + t**3 * x2
        py = u**3 * y1 + 3 * u**2 * t * cy1 + 3 * u * t**2 * cy2 + t**3 * y2
        page.mouse.move(px, py)
        page.wait_for_timeout(random.randint(4, 16))


def _click_with_bezier(page, locator, hold_ms=0, box_timeout=5000):
    box = locator.bounding_box(timeout=box_timeout)
    if not box:
        raise TimeoutError("element has no bounding box")
    tx = box["x"] + box["width"] / 2 + random.randint(-5, 5)
    ty = box["y"] + box["height"] / 2 + random.randint(-5, 5)
    sx = tx + random.randint(-80, 80)
    sy = ty + random.randint(-40, 40)
    page.mouse.move(sx, sy)
    page.wait_for_timeout(random.randint(60, 200))
    _bezier_move(page, sx, sy, tx, ty)
    page.wait_for_timeout(random.randint(30, 100))
    if hold_ms > 0:
        page.mouse.down()
        page.wait_for_timeout(hold_ms + random.randint(-200, 400))
        page.mouse.up()
    else:
        page.mouse.click(tx, ty)


def _first_visible_locator(page, selectors, timeout=10000):
    deadline = time.time() + timeout / 1000
    last_error = None
    while time.time() < deadline:
        for selector in selectors:
            try:
                matches = page.locator(selector)
                for index in range(min(matches.count(), 8)):
                    locator = matches.nth(index)
                    if locator.is_visible():
                        return locator
            except Exception as exc:
                last_error = exc
        page.wait_for_timeout(250)
    raise TimeoutError(f"no visible locator found: {selectors}; last_error={last_error}")


def _type_into(locator, value, delay=0):
    try:
        locator.fill("", timeout=3000)
        locator.fill(value, timeout=10000)
        return
    except Exception as fill_exc:
        last_error = fill_exc
    try:
        locator.evaluate(
            """(el) => {
                el.focus();
                if ('value' in el) el.value = '';
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }"""
        )
    except Exception as focus_exc:
        last_error = focus_exc
    try:
        locator.press("Control+A", timeout=2000)
        locator.press("Backspace", timeout=2000)
    except Exception as press_exc:
        last_error = press_exc
    try:
        if delay > 0:
            locator.type(value, delay=delay, timeout=10000)
        else:
            locator.fill(value, timeout=10000)
        return
    except Exception as type_exc:
        last_error = type_exc
    raise last_error


def _accept_outlook_consent_if_visible(page, timeout=3000) -> bool:
    try:
        consent = _first_visible_locator(page, OUTLOOK_CONSENT_SELECTORS, timeout=timeout)
    except Exception:
        return False
    page.wait_for_timeout(150)
    consent.click(timeout=30000)
    logger.info("[outlook] Consent accepted")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    return True


def _outlook_email_input(page, timeout=30000):
    deadline = time.time() + timeout / 1000
    last_error = None
    while time.time() < deadline:
        if _accept_outlook_consent_if_visible(page, timeout=500):
            page.wait_for_timeout(800)
            continue
        try:
            return _first_visible_locator(page, OUTLOOK_EMAIL_INPUT_SELECTORS, timeout=700)
        except Exception as exc:
            last_error = exc
        page.wait_for_timeout(250)

    raise TimeoutError(
        f"no Outlook email input visible after consent handling; "
        f"url={_safe_page_url(page)}; last_error={last_error}"
    )


def _click_primary(page, timeout=10000):
    button = _first_visible_locator(page, [
        '[data-testid="primaryButton"]',
        'button[type="submit"]',
        'button:has-text("Next")',
        'button:has-text("Continue")',
    ], timeout=timeout)
    errors = []
    actions = [
        lambda: button.click(timeout=2000),
        lambda: button.click(timeout=2000, force=True),
        lambda: button.evaluate("(el) => el.click()"),
        lambda: button.dispatch_event("click", timeout=1000),
    ]
    for action in actions:
        try:
            action()
            return
        except Exception as exc:
            errors.append(str(exc))
            try:
                if button.count() == 0 or not button.is_visible(timeout=100):
                    return
            except Exception:
                return
    raise TimeoutError(errors[-1] if errors else "primary button click failed")


def _password_input_visible(page) -> bool:
    try:
        _first_visible_locator(page, [
            'input[type="password"]',
            '[name="Password"]',
            '[name="passwd"]',
            'input[aria-label*="Password"]',
            'input[autocomplete="new-password"]',
        ], timeout=400)
        return True
    except Exception:
        return False


def _visible_text_present(page, texts) -> bool:
    for text in texts:
        try:
            matches = page.get_by_text(text)
            for index in range(min(matches.count(), 6)):
                if matches.nth(index).is_visible(timeout=100):
                    return True
        except Exception:
            continue
    return False


def _is_mailbox_url(current_url: str) -> bool:
    lowered = (current_url or "").lower()
    return (
        "outlook.live.com/mail" in lowered
        or "outlook.office.com/mail" in lowered
        or "outlook.office365.com/mail" in lowered
    )


def _wait_for_mailbox_success(page, timeout=60000) -> bool:
    deadline = time.time() + timeout / 1000
    last_url = ""
    while time.time() < deadline:
        try:
            current_url = page.url
        except Exception as exc:
            if _is_target_closed(exc):
                return False
            raise

        if current_url != last_url:
            last_url = current_url
            logger.info("[outlook] Waiting for mailbox page, current url: %s", current_url.split("?", 1)[0])

        if _is_mailbox_url(current_url):
            return True

        if _visible_text_present(page, [
            'unusual activity',
            'temporarily restricted',
            'site is under maintenance',
            'site is being maintained',
            'Try again later',
            'Something went wrong',
        ]):
            return False

        page.wait_for_timeout(1000)
    return False


def _email_unavailable_visible(page) -> bool:
    return _visible_text_present(page, [
        "is already taken",
        "already taken",
        "Someone already has",
        "not available",
        "unavailable",
    ])


def _suggested_email_locals(page, current_local: str, suffix: str) -> list[str]:
    try:
        suggestions = page.evaluate(
            """({ currentLocal, suffix }) => {
                const seen = new Set();
                const out = [];
                const add = (raw) => {
                    let text = String(raw || '').trim().toLowerCase();
                    if (!text) return;
                    if (text.endsWith(suffix)) {
                        text = text.slice(0, -suffix.length);
                    }
                    if (
                        text !== currentLocal &&
                        /^(?=.*\\d)[a-z][a-z0-9_-]{5,32}$/.test(text) &&
                        !seen.has(text)
                    ) {
                        seen.add(text);
                        out.push(text);
                    }
                };
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                document.querySelectorAll('button, [role="button"], a, span, div').forEach((el) => {
                    if (!visible(el)) return;
                    const raw = (el.innerText || el.textContent || '').trim();
                    raw.split(/\\s+/).forEach(add);
                });
                return out.slice(0, 6);
            }""",
            {"currentLocal": current_local.lower(), "suffix": suffix.lower()},
        )
        return [str(value).strip().lower() for value in suggestions if str(value).strip()]
    except Exception:
        return []


def _wait_for_email_outcome(page, current_local: str, suffix: str, timeout=30000) -> str:
    deadline = time.time() + timeout / 1000
    unavailable_check_at = time.time() + 3.0
    while time.time() < deadline:
        if _password_input_visible(page):
            return "password"
        if time.time() >= unavailable_check_at:
            if _email_unavailable_visible(page) or _suggested_email_locals(page, current_local, suffix):
                return "unavailable"
        page.wait_for_timeout(250)
    if _password_input_visible(page):
        return "password"
    if _email_unavailable_visible(page) or _suggested_email_locals(page, current_local, suffix):
        return "unavailable"
    return "unknown"


def _select_birth_value(page, field_name, value, option_texts):
    field = _first_visible_locator(page, [
        f'[name="{field_name}"]',
        f'select[name="{field_name}"]',
        f'[id="{field_name}"]',
        f'[aria-labelledby*="{field_name}"]',
    ], timeout=10000)
    try:
        field.select_option(value=value, timeout=1500)
        return
    except Exception:
        pass

    def _open_dropdown():
        errors = []
        actions = [
            lambda: field.click(timeout=2000, force=True),
            lambda: field.evaluate("(el) => el.click()"),
            lambda: field.press("Enter", timeout=2000),
            lambda: field.press("Space", timeout=2000),
            lambda: field.press("Alt+ArrowDown", timeout=2000),
        ]
        for action in actions:
            try:
                action()
                page.wait_for_timeout(150)
                if page.locator('[role="option"]').count() > 0:
                    return
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise TimeoutError("; ".join(errors[-2:]))

    option_selectors = []
    for text in option_texts:
        escaped = str(text).replace('"', '\\"')
        option_selectors.extend([
            f'[role="option"]:text-is("{escaped}")',
            f'[role="option"]:has-text("{escaped}")',
            f'option:text-is("{escaped}")',
            f'option:has-text("{escaped}")',
        ])

    _open_dropdown()
    try:
        option = _first_visible_locator(page, option_selectors, timeout=2500)
        try:
            option.click(timeout=2000, force=True)
        except Exception:
            option.evaluate("(el) => el.click()")
        return
    except Exception:
        pass

    try:
        index = max(0, int(value) - 1)
        options = page.locator('[role="option"]')
        if options.count() > index:
            option = options.nth(index)
            try:
                option.click(timeout=2000, force=True)
            except Exception:
                option.evaluate("(el) => el.click()")
            return
    except Exception:
        pass

    raise TimeoutError(f"could not select {field_name}={value}")


def _click_first_if_visible(page, selectors, timeout=1000) -> bool:
    try:
        _first_visible_locator(page, selectors, timeout=timeout).click(timeout=5000, force=True)
        return True
    except Exception:
        return False


def _oauth_code_from_url(current_url: str) -> tuple[str, str]:
    parsed = urlparse(current_url)
    query = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment)
    params = {**fragment, **query}
    if "error" in params:
        error = params.get("error", ["oauth_error"])[0]
        description = params.get("error_description", [""])[0]
        return "", f"{error}: {description}".strip()
    return params.get("code", [""])[0], ""


def _safe_email_file_part(email: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", email.strip().lower())


def _read_manual_oauth_value(kind: str, email: str, results_dir: str) -> str:
    if kind == "proof_email":
        env_names = ["OUTLOOK_REGISTER_OAUTH_PROOF_EMAIL"]
        file_env_names = ["OUTLOOK_REGISTER_OAUTH_PROOF_EMAIL_FILE"]
        file_names = [
            f"oauth_proof_email_{_safe_email_file_part(email)}.txt",
            "oauth_proof_email.txt",
        ]
    else:
        env_names = ["OUTLOOK_REGISTER_OAUTH_VERIFICATION_CODE", "OUTLOOK_REGISTER_OAUTH_CODE"]
        file_env_names = ["OUTLOOK_REGISTER_OAUTH_VERIFICATION_CODE_FILE", "OUTLOOK_REGISTER_OAUTH_CODE_FILE"]
        file_names = [
            f"oauth_code_{_safe_email_file_part(email)}.txt",
            "oauth_code.txt",
        ]

    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value

    candidate_paths = []
    for name in file_env_names:
        path = os.environ.get(name, "").strip()
        if path:
            candidate_paths.append(path)
    if results_dir:
        candidate_paths.extend(str(os.path.join(results_dir, name)) for name in file_names)

    for path in candidate_paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
                if value:
                    return value.splitlines()[0].strip()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("[oauth] failed to read manual %s file %s: %s", kind, path, exc)
    return ""


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _complete_microsoft_oauth_page(
    page,
    email: str,
    password: str,
    redirect_url: str,
    timeout=160,
    captured_url_fn: Callable[[], str] | None = None,
    results_dir: str = "",
) -> tuple[str, str]:
    deadline = time.time() + timeout
    last_url = ""
    while time.time() < deadline:
        if captured_url_fn:
            captured_url = captured_url_fn()
            if captured_url:
                code, error = _oauth_code_from_url(captured_url)
                if code or error:
                    return code, error

        current_url = page.url
        if current_url != last_url:
            last_url = current_url
            logger.info("[oauth] page url changed: %s", current_url.split("?", 1)[0])
        parsed_url = urlparse(current_url)
        if parsed_url.netloc.lower() == "account.live.com" and parsed_url.path.lower().startswith("/abuse"):
            return "", "NEEDS_MANUAL_VERIFICATION: Microsoft account redirected to account.live.com/Abuse"

        code, error = _oauth_code_from_url(current_url)
        if code:
            return code, ""
        if error:
            return "", error

        progressed = False

        try:
            proof_input = _first_visible_locator(page, [
                '#proof-confirmation-email-input',
                'input[name="proofConfirmationEmail"]',
                'input[aria-label*="Email"]',
            ], timeout=700)
            proof_email = _read_manual_oauth_value("proof_email", email, results_dir)
            if proof_email:
                _type_into(proof_input, proof_email)
                _click_first_if_visible(page, [
                    'button[type="submit"]',
                    'button:has-text("Send code")',
                ], timeout=3000)
                progressed = True
            elif not _click_first_if_visible(page, [
                'button:has-text("Use password")',
                'button:has-text("Use your password")',
                '[role="button"]:has-text("Use password")',
                '[role="button"]:has-text("Use your password")',
            ], timeout=700):
                return "", (
                    "NEEDS_MANUAL_VERIFICATION: Microsoft asked to verify recovery email; "
                    "provide OUTLOOK_REGISTER_OAUTH_PROOF_EMAIL or oauth_proof_email_<email>.txt"
                )
            else:
                progressed = True
        except Exception:
            pass

        try:
            code_input = _first_visible_locator(page, [
                'input[name="otc"]',
                '#idTxtBx_SAOTCC_OTC',
                'input[autocomplete="one-time-code"]',
                'input[aria-label*="code"]',
                'input[aria-label*="Code"]',
            ], timeout=700)
            verification_code = _read_manual_oauth_value("verification_code", email, results_dir)
            if not verification_code:
                return "", (
                    "NEEDS_MANUAL_VERIFICATION: Microsoft asked for email verification code; "
                    "provide OUTLOOK_REGISTER_OAUTH_VERIFICATION_CODE or oauth_code_<email>.txt"
                )
            _type_into(code_input, verification_code)
            _click_first_if_visible(page, [
                'button[type="submit"]',
                '#idSubmit_SAOTCC_Continue',
                '#idSIButton9',
                'button:has-text("Verify")',
                'button:has-text("Next")',
            ], timeout=3000)
            progressed = True
        except Exception:
            pass

        if _click_first_if_visible(page, [
            'button:has-text("Use password")',
            'button:has-text("Use your password")',
            '[role="button"]:has-text("Use password")',
            '[role="button"]:has-text("Use your password")',
        ], timeout=700):
            progressed = True

        if _click_first_if_visible(page, [
            f'[data-test-id="{email}"]',
            f'[aria-label*="{email}"]',
            f'div[role="button"]:has-text("{email}")',
        ], timeout=700):
            progressed = True

        try:
            login = _first_visible_locator(page, [
                '[name="loginfmt"]',
                'input[type="email"]',
                'input[autocomplete="username"]',
            ], timeout=700)
            _type_into(login, email)
            _click_first_if_visible(page, [
                '#idSIButton9',
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Next")',
            ], timeout=3000)
            progressed = True
        except Exception:
            pass

        try:
            password_input = _first_visible_locator(page, [
                '[name="passwd"]',
                'input[type="password"]',
                'input[autocomplete="current-password"]',
            ], timeout=700)
            _type_into(password_input, password)
            _click_first_if_visible(page, [
                '#idSIButton9',
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Sign in")',
            ], timeout=3000)
            progressed = True
        except Exception:
            pass

        if _click_first_if_visible(page, [
            '[data-testid="appConsentPrimaryButton"]',
            'button:has-text("Accept")',
            'button:has-text("Continue")',
            '#idSIButton9',
            'input[type="submit"]',
        ], timeout=700):
            progressed = True

        if redirect_url in page.url:
            code, error = _oauth_code_from_url(page.url)
            if code or error:
                return code, error

        page.wait_for_timeout(900 if progressed else 500)

    return "", "OAuth browser flow timed out before authorization code was captured"


def outlook_oauth(
    email: str,
    password: str,
    proxy: str = "",
    client_id: str = "",
    redirect_url: str = "",
    scopes: list[str] | None = None,
) -> dict:
    from browserforge.fingerprints import Screen
    from camoufox.sync_api import Camoufox

    email = email.strip().lower()
    password = password.strip()
    client_id = client_id.strip()
    redirect_url = redirect_url.strip()
    scopes = [scope.strip() for scope in (scopes or []) if scope.strip()]
    result = {
        "success": False,
        "email": email,
        "refresh_token": "",
        "access_token": "",
        "error": "",
    }
    if not email or not password:
        result["error"] = "email and password are required for OAuth"
        return result
    if not client_id or not redirect_url or not scopes:
        result["error"] = "client_id, redirect_url, and scopes are required for OAuth"
        return result

    verifier, challenge = _pkce_pair()
    authorize_url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_url,
        "response_mode": "query",
        "scope": " ".join(scopes),
        "prompt": "consent",
        "login_hint": email,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    cf_proxy = None
    if proxy:
        pp = urlparse(proxy)
        cf_proxy = {"server": f"{pp.scheme}://{pp.hostname}:{pp.port}",
                    "username": pp.username or "", "password": pp.password or ""}

    ss_dir = (
        os.environ.get("SCREENSHOT_DIR")
        or os.environ.get("OUTLOOK_REGISTER_RESULTS_DIR")
        or tempfile.gettempdir()
    )
    os.makedirs(ss_dir, exist_ok=True)
    tmp_profile = tempfile.mkdtemp(prefix="outlook_oauth_")
    logger.info("[oauth] Starting OAuth for %s", email)

    try:
        import platform as _plat
        headless = "virtual" if _plat.system() == "Linux" else False
        with Camoufox(
            headless=headless, humanize=True, persistent_context=True,
            user_data_dir=tmp_profile, screen=Screen(max_width=1920, max_height=1080),
            proxy=cf_proxy, geoip=True,
            locale="en-US",
        ) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            captured_url = ""

            def on_request(request):
                nonlocal captured_url
                if redirect_url in request.url and ("code=" in request.url or "error=" in request.url):
                    captured_url = request.url

            page.on("request", on_request)
            try:
                page.goto(authorize_url, timeout=30000, wait_until="domcontentloaded")
                code, error = _complete_microsoft_oauth_page(
                    page,
                    email,
                    password,
                    redirect_url,
                    captured_url_fn=lambda: captured_url,
                    results_dir=ss_dir,
                )
            finally:
                try:
                    page.remove_listener("request", on_request)
                except Exception:
                    pass
            if not code:
                page.screenshot(path=os.path.join(ss_dir, "outlook_oauth_error.png"))
                result["error"] = error or "OAuth authorization code was not captured"
                logger.error("[oauth] %s", result["error"])
                return result

        token_resp = requests.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_url,
                "code_verifier": verifier,
                "scope": " ".join(scopes),
            },
            timeout=30,
        )
        if token_resp.status_code != 200:
            result["error"] = f"token exchange failed status={token_resp.status_code}: {token_resp.text[:500]}"
            logger.error("[oauth] %s", result["error"])
            return result

        tokens = token_resp.json()
        refresh_token = str(tokens.get("refresh_token", "")).strip()
        if not refresh_token:
            result["error"] = "token exchange succeeded but no refresh_token was returned"
            logger.error("[oauth] %s", result["error"])
            return result

        result["success"] = True
        result["refresh_token"] = refresh_token
        result["access_token"] = str(tokens.get("access_token", "")).strip()
        logger.info("[oauth] OAuth successful for %s", email)
        return result
    except Exception as exc:
        result["error"] = str(exc)
        logger.exception("[oauth] OAuth failed")
        return result
    finally:
        shutil.rmtree(tmp_profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# CAPTCHA handler (auto press-and-hold only)
# ---------------------------------------------------------------------------

def _handle_captcha(page, max_retries=10) -> bool:
    """Handle CAPTCHA, searching across all frames."""

    def _find_in_any_frame(selectors, timeout=5000, locator_timeout=120, poll_ms=120):
        """Search for a visible locator across page and all frames."""
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            for frame in _captcha_frames():
                if time.time() >= deadline:
                    return None
                for sel in selectors:
                    remaining_ms = int((deadline - time.time()) * 1000)
                    if remaining_ms <= 0:
                        return None
                    try:
                        loc = frame.locator(sel).first
                        if loc.is_visible(timeout=max(1, min(locator_timeout, remaining_ms))):
                            return loc
                    except Exception as exc:
                        if _is_target_closed(exc):
                            if _page_is_closed(page):
                                raise
                            logger.debug("[captcha] frame target closed while searching; retrying")
                            continue
            remaining_ms = int((deadline - time.time()) * 1000)
            if remaining_ms > 0:
                page.wait_for_timeout(min(poll_ms, remaining_ms))
        return None

    def _captcha_frames():
        frames = list(page.frames)
        keywords = (
            "captcha",
            "arkose",
            "funcaptcha",
            "enforcement",
            "human",
            "perimeter",
            "hsprotect",
            "px-cloud",
            "px-cdn",
        )
        selected = [frame for frame in frames if any(key in (frame.url or "").lower() for key in keywords)]
        return selected or frames

    def _find_control_in_any_frame(
        timeout=15000,
        exclude=None,
        kind_filter: Optional[str] = None,
        action_filter: Optional[str] = None,
        randomize=False,
    ):
        """Find the next clickable CAPTCHA control without slow Playwright selector scans."""
        deadline = time.time() + timeout / 1000
        script = """
        (opts) => {
            const exclude = opts.exclude || {};
            const kindFilter = opts.kindFilter || '';
            const actionFilter = opts.actionFilter || '';
            const attr = 'data-nb-captcha-control';
            document.querySelectorAll('[' + attr + '="1"]').forEach((el) => el.removeAttribute(attr));
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' &&
                    style.display !== 'none' &&
                    el.getAttribute('aria-disabled') !== 'true' &&
                    !el.disabled;
            };
            const candidates = Array.from(document.querySelectorAll(
                'button, a[role="button"], [role="button"], input[type="button"], input[type="submit"], [aria-label]'
            ));
            const blocked = (el, kind, text) => {
                if (exclude &&
                    exclude.clickId &&
                    el.getAttribute('data-nb-captcha-click-id') === exclude.clickId &&
                    kind === exclude.kind &&
                    text === exclude.text) {
                    return true;
                }
                return false;
            };
            const actionType = (text) => {
                if (/press again|try again/i.test(text)) return 'again';
                if (/press\\s*&\\s*hold|press and hold/i.test(text)) return 'hold';
                if (/next|continue/i.test(text)) return 'advance';
                if (/press/i.test(text)) return 'press';
                return '';
            };
            const matches = [];
            for (const el of candidates) {
                if (!visible(el)) continue;
                const text = [
                    el.getAttribute('aria-label') || '',
                    el.innerText || '',
                    el.value || '',
                    el.textContent || '',
                ].join(' ').trim();
                if (/Human Challenge completed|challenge completed/i.test(text)) continue;
                if (/Accessible challenge/i.test(text)) {
                    if (kindFilter !== 'action' && !blocked(el, 'accessible', text)) {
                        matches.push({el, kind: 'accessible'});
                    }
                    continue;
                }
                if (/Next|Continue|Press|again/i.test(text)) {
                    const action = actionType(text);
                    if (kindFilter !== 'accessible' &&
                        (!actionFilter || action === actionFilter) &&
                        !blocked(el, 'action', text)) {
                        matches.push({el, kind: 'action', action});
                    }
                }
            }
            let match = null;
            if (opts.randomize) {
                const actions = matches.filter((item) => item.kind === 'action');
                const accessibles = matches.filter((item) => item.kind === 'accessible');
                const buckets = [];
                if (actions.length) buckets.push(actions[Math.floor(Math.random() * actions.length)]);
                if (accessibles.length) buckets.push(accessibles[Math.floor(Math.random() * accessibles.length)]);
                if (buckets.length) match = buckets[Math.floor(Math.random() * buckets.length)];
            }
            if (!match) {
                match = matches.find((item) => item.kind === 'action') || matches.find((item) => item.kind === 'accessible');
            }
            if (match) {
                match.el.setAttribute(attr, '1');
                match.el.setAttribute('data-nb-captcha-kind', match.kind);
                match.el.setAttribute('data-nb-captcha-action', match.action || '');
                return true;
            }
            return false;
        }
        """
        while time.time() < deadline:
            for frame in _captcha_frames():
                if time.time() >= deadline:
                    return None
                try:
                    if frame.evaluate(script, {
                        "exclude": exclude or {},
                        "kindFilter": kind_filter or "",
                        "actionFilter": action_filter or "",
                        "randomize": bool(randomize),
                    }):
                        loc = frame.locator('[data-nb-captcha-control="1"]').first
                        if loc.is_visible(timeout=100):
                            return loc
                except Exception as exc:
                    if _is_target_closed(exc):
                        if _page_is_closed(page):
                            raise
                        continue
            remaining_ms = int((deadline - time.time()) * 1000)
            if remaining_ms > 0:
                page.wait_for_timeout(min(80, remaining_ms))
        return None

    def _locator_text(locator) -> str:
        try:
            return str(locator.evaluate(
                """(el) => [
                    el.getAttribute('aria-label') || '',
                    el.innerText || '',
                    el.value || '',
                    el.textContent || '',
                ].join(' ')"""
            ) or "")
        except Exception:
            return ""

    def _locator_state(locator) -> dict:
        return locator.evaluate(
            """(el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return {
                    visible: rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' &&
                        style.display !== 'none',
                    text: [
                        el.getAttribute('aria-label') || '',
                        el.innerText || '',
                        el.value || '',
                        el.textContent || '',
                    ].join(' ').trim(),
                    ariaDisabled: el.getAttribute('aria-disabled') || '',
                    disabled: Boolean(el.disabled),
                };
            }"""
        )

    def _challenge_completed_visible() -> bool:
        script = """
        () => {
            const visible = (el) => {
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' &&
                    style.display !== 'none';
            };
            const bodyText = document.body ? (document.body.innerText || '') : '';
            const ariaText = Array.from(document.querySelectorAll('[aria-label]'))
                .filter((el) => {
                    return visible(el);
                })
                .map((el) => [
                    el.getAttribute('aria-label') || '',
                ].join(' '))
                .join(' ');
            const text = `${bodyText} ${ariaText}`;
            return /Human Challenge completed|challenge completed/i.test(text);
        }
        """
        for frame in _captcha_frames():
            try:
                if frame.evaluate(script):
                    return True
            except Exception:
                continue
        return False

    def _retry_signal_visible() -> bool:
        script = """
        () => {
            const visible = (el) => {
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' &&
                    style.display !== 'none';
            };
            const bodyText = document.body ? (document.body.innerText || '') : '';
            const ariaText = Array.from(document.querySelectorAll('[aria-label]'))
                .filter((el) => {
                    return visible(el);
                })
                .map((el) => [
                    el.getAttribute('aria-label') || '',
                ].join(' '))
                .join(' ');
            const text = `${bodyText} ${ariaText}`;
            return /please try again|try again|retry/i.test(text);
        }
        """
        for frame in _captcha_frames():
            try:
                if frame.evaluate(script):
                    return True
            except Exception:
                continue
        return False

    def _control_kind(locator) -> str:
        try:
            return str(locator.get_attribute("data-nb-captcha-kind", timeout=100) or "")
        except Exception:
            return ""

    def _control_action(locator) -> str:
        try:
            return str(locator.get_attribute("data-nb-captcha-action", timeout=100) or "")
        except Exception:
            return ""

    def _summarize_control_text(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()[:80]

    def _mark_clicked_control(locator, kind: str, text: str) -> dict:
        click_id = f"{time.time_ns()}-{random.randint(1000, 9999)}"
        locator.evaluate(
            """(el, clickId) => el.setAttribute('data-nb-captcha-click-id', clickId)""",
            click_id,
        )
        return {"clickId": click_id, "kind": kind, "text": text}

    def _quick_click_locator(locator, timeout=300):
        box = locator.bounding_box(timeout=timeout)
        if not box:
            locator.dispatch_event("click", timeout=timeout)
            return
        x = box["x"] + box["width"] / 2 + random.randint(-2, 2)
        y = box["y"] + box["height"] / 2 + random.randint(-2, 2)
        page.mouse.click(x, y)

    def _captcha_hold_ms(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, str(default)))
        except ValueError:
            return default

    captcha_min_hold_ms = max(3000, _captcha_hold_ms("OUTLOOK_REGISTER_CAPTCHA_MIN_HOLD_MS", 4500))
    captcha_max_hold_ms = max(
        captcha_min_hold_ms + 1000,
        _captcha_hold_ms("OUTLOOK_REGISTER_CAPTCHA_MAX_HOLD_MS", 12000),
    )

    def _hold_until_component_changes(locator, timeout=300, min_hold_ms=None, max_hold_ms=None):
        if min_hold_ms is None:
            min_hold_ms = captcha_min_hold_ms
        if max_hold_ms is None:
            max_hold_ms = captcha_max_hold_ms
        box = locator.bounding_box(timeout=timeout)
        if not box:
            raise TimeoutError("captcha action has no bounding box")
        initial = _locator_state(locator)
        x = box["x"] + box["width"] / 2 + random.randint(-2, 2)
        y = box["y"] + box["height"] / 2 + random.randint(-2, 2)
        try:
            locator.scroll_into_view_if_needed(timeout=500)
        except Exception:
            pass
        page.mouse.move(x, y, steps=random.randint(3, 7))
        page.mouse.down()
        reason = "max timeout"
        started_at = time.time()
        try:
            while (time.time() - started_at) * 1000 < max_hold_ms:
                page.wait_for_timeout(120)
                elapsed_ms = int((time.time() - started_at) * 1000)
                if elapsed_ms < min_hold_ms:
                    continue
                if _challenge_completed_visible():
                    reason = "challenge completed"
                    break
                try:
                    current = _locator_state(locator)
                except Exception as exc:
                    if (
                        "Frame was detached" in str(exc)
                        or "Execution context was destroyed" in str(exc)
                        or _is_target_closed(exc)
                    ):
                        reason = "component detached"
                        break
                    raise
                if not current.get("visible", False):
                    reason = "component hidden"
                    break
                if current.get("disabled") or current.get("ariaDisabled") == "true":
                    reason = "component disabled"
                    break
                current_text = str(current.get("text") or "")
                if re.search(r"press again|try again|next|continue|completed|please wait", current_text, re.I):
                    reason = "component advanced"
                    break
        finally:
            try:
                page.mouse.up()
            except Exception:
                pass
        return reason

    def _wait_after_captcha_click(
        previous_url: str,
        clicked_control: Optional[dict] = None,
        timeout=15000,
        next_kind: Optional[str] = "action",
        next_action: Optional[str] = None,
        wait_for_control=True,
    ) -> str:
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            try:
                current_url = page.url
            except Exception as exc:
                if _is_target_closed(exc):
                    return "closed"
                raise

            if _is_mailbox_url(current_url):
                return "mailbox"
            if current_url != previous_url:
                return "url_changed"

            if _retry_signal_visible():
                return "retry_signal"

            if wait_for_control:
                control = _find_control_in_any_frame(
                    timeout=250,
                    exclude=clicked_control,
                    kind_filter=next_kind,
                    action_filter=next_action,
                )
                if control:
                    return "control"

            remaining_ms = int((deadline - time.time()) * 1000)
            if remaining_ms > 0:
                page.wait_for_timeout(min(250, remaining_ms))
        return "timeout"

    max_retry_signals = max(1, int(max_retries))
    max_interaction_steps = max(20, max_retry_signals * 6)
    retry_signals = 0
    step = 0
    next_search_kind: Optional[str] = None
    next_search_action: Optional[str] = None

    try:
        while retry_signals < max_retry_signals and step < max_interaction_steps:
            step += 1
            logger.info(
                "[captcha] Waiting for clickable challenge control (step %d, retry %d/%d)...",
                step,
                retry_signals,
                max_retry_signals,
            )
            control = _find_control_in_any_frame(
                timeout=15000,
                kind_filter=next_search_kind,
                action_filter=next_search_action,
                randomize=next_search_kind is None,
            )
            if not control:
                logger.error("[captcha] Timed out waiting for clickable challenge control")
                return False

            try:
                previous_url = page.url
                delay_ms = random.randint(500, 2000)
                logger.info("[captcha] Clickable control found; waiting %dms before click", delay_ms)
                page.wait_for_timeout(delay_ms)
                fresh_control = _find_control_in_any_frame(
                    timeout=500,
                    kind_filter=next_search_kind,
                    action_filter=next_search_action,
                    randomize=next_search_kind is None,
                )
                if fresh_control:
                    control = fresh_control
                kind = _control_kind(control)
                action = _control_action(control)
                action_text = _locator_text(control)
                logger.info(
                    "[captcha] Control ready (%s/%s: %s)",
                    kind or "unknown",
                    action or "-",
                    _summarize_control_text(action_text) or "<no text>",
                )
                clicked_control = _mark_clicked_control(control, kind, action_text)
                expected_action: Optional[str] = None

                if re.search(r"completed|please wait", action_text, re.I):
                    logger.info("[captcha] Challenge already completed; waiting for next state")
                elif kind == "accessible" or re.search(r"accessible challenge", action_text, re.I):
                    _quick_click_locator(control, timeout=300)
                    logger.info("[captcha] Accessible challenge clicked")
                    expected_action = "again"
                elif action == "again" or re.search(r"press again|try again", action_text, re.I):
                    _quick_click_locator(control, timeout=300)
                    logger.info("[captcha] Press again clicked")
                    expected_action = None
                elif action == "hold" or re.search(r"press", action_text, re.I):
                    logger.info(
                        "[captcha] Press-and-hold action button until component changes (min %dms, max %dms)",
                        captcha_min_hold_ms,
                        captcha_max_hold_ms,
                    )
                    reason = _hold_until_component_changes(control, timeout=300)
                    logger.info("[captcha] Action button held until %s", reason)
                    expected_action = None
                else:
                    _quick_click_locator(control, timeout=300)
                    logger.info("[captcha] Action button clicked")

                next_state = _wait_after_captcha_click(
                    previous_url,
                    clicked_control=clicked_control,
                    timeout=15000,
                    next_kind="action",
                    next_action=expected_action,
                    wait_for_control=True,
                )
                logger.info("[captcha] Next state after click: %s", next_state)
                if next_state in {"mailbox", "url_changed", "success_signal"}:
                    return True
                if next_state == "retry_signal":
                    retry_signals += 1
                    if retry_signals >= max_retry_signals:
                        logger.error(
                            "[captcha] Challenge retry signals exhausted (%d/%d)",
                            retry_signals,
                            max_retry_signals,
                        )
                        return False
                    logger.warning(
                        "[captcha] Challenge returned retry signal (retry %d/%d); retrying",
                        retry_signals,
                        max_retry_signals,
                    )
                    next_search_kind = None
                    next_search_action = None
                    continue
                if next_state == "control":
                    if action in {"again", "hold"}:
                        retry_signals += 1
                        if retry_signals >= max_retry_signals:
                            logger.error(
                                "[captcha] Challenge retry controls exhausted (%d/%d)",
                                retry_signals,
                                max_retry_signals,
                            )
                            return False
                        logger.warning(
                            "[captcha] Challenge presented another control after %s (retry %d/%d); continuing",
                            action,
                            retry_signals,
                            max_retry_signals,
                        )
                        next_search_kind = None
                        next_search_action = None
                    else:
                        next_search_kind = "action"
                        next_search_action = expected_action
                    continue
                logger.error("[captcha] Timed out waiting for next CAPTCHA state after action")
                return False
            except Exception as e:
                if "Frame was detached" in str(e) or "Execution context was destroyed" in str(e):
                    logger.info("[captcha] Challenge frame changed during action, retrying...")
                    continue
                if _is_target_closed(e):
                    if _page_is_closed(page):
                        raise
                    logger.warning("[captcha] Action button frame closed, retrying...")
                    continue
                logger.error("[captcha] Action button click failed: %s", e)
                return False
    except Exception as e:
        if _is_target_closed(e):
            if _page_is_closed(page):
                logger.error("[captcha] Browser page/context closed unexpectedly during CAPTCHA")
            else:
                logger.error("[captcha] CAPTCHA target closed unexpectedly during CAPTCHA")
            return False
        raise

    if retry_signals >= max_retry_signals:
        logger.error("[captcha] retry signals exhausted")
    else:
        logger.error("[captcha] interaction steps exhausted before CAPTCHA reached a final state")
    return False


# ---------------------------------------------------------------------------
# Main registration flow (mirrors OutlookRegister base_controller logic)
# ---------------------------------------------------------------------------

def outlook_register(
    proxy: str = "",
    email_suffix: str = "@outlook.com",
    max_captcha_retries: int = 10,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
    debug: bool = False,
) -> dict:
    from camoufox.sync_api import Camoufox
    from browserforge.fingerprints import Screen

    email_local = _gen_email_local()
    full_email = email_local + email_suffix
    password = _gen_password()
    first_name, last_name = _gen_name()
    year = str(random.randint(1960, 2005))
    month = str(random.randint(1, 12))
    day = str(random.randint(1, 28))

    wait_ms = BOT_PROTECTION_WAIT * 1000

    result = {"success": False, "email": full_email, "password": password, "error": ""}

    cf_proxy = None
    if proxy:
        pp = urlparse(proxy)
        cf_proxy = {"server": f"{pp.scheme}://{pp.hostname}:{pp.port}",
                     "username": pp.username or "", "password": pp.password or ""}

    tmp_profile = tempfile.mkdtemp(prefix="outlook_reg_")
    logger.info("[outlook] Starting: %s", full_email)

    ss_dir = (
        os.environ.get("SCREENSHOT_DIR")
        or os.environ.get("OUTLOOK_REGISTER_RESULTS_DIR")
        or tempfile.gettempdir()
    )
    os.makedirs(ss_dir, exist_ok=True)

    def _debug_pause(msg: str = ""):
        """In debug mode, pause and keep the browser open for inspection."""
        if not debug:
            return
        logger.info("[debug] %s", msg or "Pausing for inspection. Press Enter to continue...")
        try:
            input("[DEBUG] Press Enter to close browser and exit...")
        except EOFError:
            pass

    try:
        import platform as _plat
        headless = "virtual" if _plat.system() == "Linux" else (False if debug else False)

        with Camoufox(
            headless=False if debug else headless, humanize=True, persistent_context=True,
            user_data_dir=tmp_profile, screen=Screen(max_width=1920, max_height=1080),
            proxy=cf_proxy, geoip=True, locale="en-US",
        ) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # [1] Open Outlook signup via prompt=create_account
            logger.info("[outlook] Opening signup...")
            try:
                page.goto("https://outlook.live.com/mail/0/?prompt=create_account",
                          timeout=60000, wait_until="domcontentloaded")
                start_time = time.time()
                page.wait_for_timeout(int(0.1 * wait_ms))
                if not _accept_outlook_consent_if_visible(page, timeout=5000):
                    logger.info("[outlook] Consent screen not shown, continuing")
            except Exception as e:
                page.screenshot(path=os.path.join(ss_dir, "outlook_no_consent.png"))
                result["error"] = f"IP quality issue, cannot enter signup: {e}"
                logger.error("[outlook] %s", result["error"])
                _debug_pause(result["error"])
                return result

            # [2] Fill email
            logger.info("[outlook] Filling email: %s", full_email)
            try:
                # Switch domain if hotmail
                if email_suffix == "@hotmail.com":
                    _outlook_email_input(page, timeout=30000)
                    page.get_by_text("@outlook.com").click(timeout=10000)
                    page.locator('[role="option"]:text-is("@hotmail.com")').click()
                attempted_locals: set[str] = set()
                max_email_attempts = int(os.environ.get("OUTLOOK_REGISTER_EMAIL_ATTEMPTS", "5"))
                email_input_timeout = int(os.environ.get("OUTLOOK_REGISTER_EMAIL_INPUT_TIMEOUT_MS", "60000"))
                for attempt in range(1, max_email_attempts + 1):
                    full_email = email_local + email_suffix
                    result["email"] = full_email
                    attempted_locals.add(email_local.lower())
                    logger.info("[outlook] Trying email (%d/%d): %s", attempt, max_email_attempts, full_email)

                    email_input = _outlook_email_input(page, timeout=email_input_timeout)
                    _type_into(email_input, email_local, delay=int(0.002 * wait_ms))
                    try:
                        actual_local = email_input.input_value(timeout=2000).strip().lower()
                        if actual_local != email_local.lower():
                            logger.warning(
                                "[outlook] Email input mismatch; expected=%s actual=%s; refilling",
                                email_local,
                                actual_local,
                            )
                            email_input.fill(email_local, timeout=5000)
                    except Exception:
                        pass

                    _click_primary(page, timeout=5000)
                    page.wait_for_timeout(1000)
                    if not _password_input_visible(page) and not _email_unavailable_visible(page):
                        try:
                            email_input.press("Enter", timeout=2000)
                        except Exception:
                            pass
                    outcome = _wait_for_email_outcome(page, email_local, email_suffix, timeout=30000)
                    if outcome == "password":
                        break

                    if outcome != "unavailable":
                        page.screenshot(path=os.path.join(ss_dir, "outlook_email_error.png"))
                        result["error"] = "Email submit did not reach password page"
                        logger.error("[outlook] %s", result["error"])
                        _debug_pause(result["error"])
                        return result

                    suggestions = [
                        suggestion for suggestion in _suggested_email_locals(page, email_local, email_suffix)
                        if suggestion.lower() not in attempted_locals
                    ]
                    if suggestions:
                        email_local = suggestions[0]
                        logger.info("[outlook] Email unavailable; retrying suggested local-part: %s", email_local)
                    elif attempt < max_email_attempts:
                        email_local = _gen_email_local()
                        logger.info("[outlook] Email unavailable; retrying generated local-part: %s", email_local)
                    else:
                        page.screenshot(path=os.path.join(ss_dir, "outlook_email_error.png"))
                        result["error"] = f"No available Outlook email accepted after {max_email_attempts} attempts"
                        logger.error("[outlook] %s", result["error"])
                        _debug_pause(result["error"])
                        return result
                else:
                    page.screenshot(path=os.path.join(ss_dir, "outlook_email_error.png"))
                    result["error"] = f"No available Outlook email accepted after {max_email_attempts} attempts"
                    logger.error("[outlook] %s", result["error"])
                    _debug_pause(result["error"])
                    return result
            except Exception as e:
                page.screenshot(path=os.path.join(ss_dir, "outlook_email_error.png"))
                result["error"] = f"Email fill failed: {e}"
                logger.error("[outlook] %s", result["error"])
                _debug_pause(result["error"])
                return result

            # [3] Fill password
            logger.info("[outlook] Filling password...")
            try:
                password_input = _first_visible_locator(page, [
                    'input[type="password"]',
                    '[name="Password"]',
                    '[name="passwd"]',
                    'input[aria-label*="Password"]',
                    'input[autocomplete="new-password"]',
                ], timeout=30000)
                _type_into(password_input, password, delay=int(0.004 * wait_ms))
                page.wait_for_timeout(int(0.02 * wait_ms))
                _click_primary(page, timeout=5000)
                page.wait_for_timeout(int(0.03 * wait_ms))
            except Exception as e:
                page.screenshot(path=os.path.join(ss_dir, "outlook_pwd_error.png"))
                result["error"] = f"Password fill failed: {e}"
                logger.error("[outlook] %s", result["error"])
                _debug_pause(result["error"])
                return result

            # [4] Fill birthday + name (same page)
            logger.info("[outlook] Filling birthday & name...")
            try:
                year_input = _first_visible_locator(page, [
                    '[name="BirthYear"]',
                    '#BirthYear',
                    'input[aria-label*="Year"]',
                    'input[inputmode="numeric"]',
                    'input[type="number"]',
                ], timeout=10000)
                _type_into(year_input, year, delay=int(0.001 * wait_ms))
                page.wait_for_timeout(int(0.02 * wait_ms))

                month_names = [
                    "January", "February", "March", "April", "May", "June",
                    "July", "August", "September", "October", "November", "December",
                ]
                _select_birth_value(
                    page,
                    "BirthMonth",
                    month,
                    [month_names[int(month) - 1], str(int(month))],
                )
                page.wait_for_timeout(int(0.04 * wait_ms))
                _select_birth_value(
                    page,
                    "BirthDay",
                    day,
                    [str(int(day))],
                )
                _click_primary(page, timeout=5000)
                page.wait_for_timeout(int(0.03 * wait_ms))

                last_name_input = _first_visible_locator(page, [
                    '#lastNameInput',
                    '[name="LastName"]',
                    '[name="lastName"]',
                    'input[aria-label*="Last"]',
                    'input[aria-label*="Surname"]',
                ], timeout=15000)
                _type_into(last_name_input, last_name, delay=int(0.002 * wait_ms))
                page.wait_for_timeout(int(0.02 * wait_ms))

                first_name_input = _first_visible_locator(page, [
                    '#firstNameInput',
                    '[name="FirstName"]',
                    '[name="firstName"]',
                    'input[aria-label*="First"]',
                    'input[aria-label*="Given"]',
                ], timeout=10000)
                _type_into(first_name_input, first_name)

                # Wait for bot protection time
                elapsed = time.time() - start_time
                if elapsed < BOT_PROTECTION_WAIT:
                    remaining_ms = int((BOT_PROTECTION_WAIT - elapsed) * 1000)
                    logger.info("[outlook] Waiting %dms for bot protection timer...", remaining_ms)
                    page.wait_for_timeout(remaining_ms)

                _click_primary(page, timeout=5000)
                # Wait for privacy link to detach (page transition)
                page.locator('span > [href="https://go.microsoft.com/fwlink/?LinkID=521839"]').wait_for(
                    state='detached', timeout=22000)
                page.wait_for_timeout(400)
            except Exception as e:
                page.screenshot(path=os.path.join(ss_dir, "outlook_form_error.png"))
                result["error"] = f"Form fill failed: {e}"
                logger.error("[outlook] %s", result["error"])
                _debug_pause(result["error"])
                return result

            # [5] Check for rate limit before captcha
            if _visible_text_present(page, [
                'unusual activity',
                'temporarily restricted',
                'site is under maintenance',
                'site is being maintained',
            ]):
                page.screenshot(path=os.path.join(ss_dir, "outlook_rate_limit.png"))
                result["error"] = "Rate limited (IP flagged)"
                logger.error("[outlook] %s", result["error"])
                _debug_pause(result["error"])
                return result

            if page.locator('iframe#enforcementFrame').count() > 0:
                page.screenshot(path=os.path.join(ss_dir, "outlook_funcaptcha.png"))
                result["error"] = "FunCaptcha type detected (not press-and-hold)"
                logger.error("[outlook] %s", result["error"])
                _debug_pause(result["error"])
                return result

            # [5.5] Check if registration passed without CAPTCHA (no risk control)
            current_url = page.url
            has_captcha_frame = any(
                'arkoselabs.com' in fr.url or 'funcaptcha' in fr.url
                or 'perimeterx' in fr.url or 'human.com' in fr.url
                for fr in page.frames if fr.url
            )
            has_captcha_elements = page.locator(
                '[aria-label="Accessible challenge"]'
            ).count() > 0

            if not has_captcha_frame and not has_captcha_elements:
                # No CAPTCHA elements found - check if we're already on the success page
                if _is_mailbox_url(current_url):
                    logger.info("[outlook] No CAPTCHA detected, already on mailbox page - registration successful!")
                    result["success"] = True
                    result["email"] = full_email
                    logger.info("[outlook] ✅ Registration successful (no CAPTCHA): %s", full_email)
                    return result
                # Wait briefly and re-check (page might be transitioning)
                page.wait_for_timeout(1000)
                current_url = page.url
                if _is_mailbox_url(current_url):
                    logger.info("[outlook] No CAPTCHA detected after wait - registration successful!")
                    result["success"] = True
                    result["email"] = full_email
                    logger.info("[outlook] ✅ Registration successful (no CAPTCHA): %s", full_email)
                    return result

            # [6] Handle CAPTCHA until success or max attempts are exhausted.
            logger.info("[outlook] Handling CAPTCHA...")
            captcha_ok = _handle_captcha(page, max_retries=max_captcha_retries)
            if not captcha_ok:
                try:
                    page.screenshot(path=os.path.join(ss_dir, "outlook_captcha_fail.png"))
                except Exception:
                    pass
                result["error"] = "CAPTCHA failed"
                logger.error("[outlook] %s", result["error"])
                _debug_pause(result["error"])
                return result

            # [7] Confirm registration reached a real mailbox page before saving.
            if not _wait_for_mailbox_success(page, timeout=60000):
                try:
                    page.screenshot(path=os.path.join(ss_dir, "outlook_registration_unconfirmed.png"))
                except Exception:
                    pass
                result["error"] = "Registration not confirmed after CAPTCHA"
                logger.error("[outlook] %s", result["error"])
                _debug_pause(result["error"])
                return result

            result["success"] = True
            result["email"] = full_email
            logger.info("[outlook] ✅ Registration successful: %s", full_email)
            _debug_pause("Registration successful! Browser kept open for inspection.")

    except Exception as e:
        result["error"] = str(e)
        logger.exception("[outlook] Registration failed")
        _debug_pause(f"Registration failed: {e}")
    finally:
        try:
            shutil.rmtree(tmp_profile, ignore_errors=True)
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Camoufox Outlook Registration")
    parser.add_argument("--proxy", default=os.environ.get("OUTLOOK_REGISTER_PROXY", ""), help="Proxy URL")
    parser.add_argument("--suffix", default=os.environ.get("OUTLOOK_REGISTER_EMAIL_SUFFIX", "@outlook.com"), help="Email suffix")
    parser.add_argument("--max-retries", type=int, default=int(os.environ.get("OUTLOOK_REGISTER_MAX_CAPTCHA_RETRIES", "10")), help="Max CAPTCHA retries")
    parser.add_argument("--results-dir", default=os.environ.get("OUTLOOK_REGISTER_RESULTS_DIR", ""), help="Directory to output results")
    parser.add_argument("--debug", action="store_true", default=False, help="Keep browser open on failure for debugging")
    args = parser.parse_args()

    result = outlook_register(proxy=args.proxy, email_suffix=args.suffix,
                              max_captcha_retries=args.max_retries, debug=args.debug)

    if args.results_dir:
        os.makedirs(args.results_dir, exist_ok=True)
        error_file = os.path.join(args.results_dir, "last_registration_error.txt")
        if result["success"]:
            try:
                if os.path.exists(error_file):
                    os.remove(error_file)
            except Exception:
                pass
        else:
            try:
                with open(error_file, "w", encoding="utf-8") as f:
                    f.write(str(result.get("error") or "registration failed").strip() + "\n")
            except Exception as e:
                logger.error(f"[outlook] Failed to save registration error: {e}")

    print("\n" + "=" * 50)
    if result["success"]:
        print(f"✅ Registration successful!")
        print(f"   Email:    {result['email']}")
        print(f"   Password: {result['password']}")
        if args.results_dir:
            os.makedirs(args.results_dir, exist_ok=True)
            out_file = os.path.join(args.results_dir, "unlogged_email.txt")
            try:
                with open(out_file, "a", encoding="utf-8") as f:
                    f.write(f"{result['email']}:{result['password']}\n")
                logger.info(f"[outlook] Saved result to {out_file}")
            except Exception as e:
                logger.error(f"[outlook] Failed to save result: {e}")
    else:
        print(f"❌ Registration failed: {result['error']}")
        print(f"   Email:    {result['email']}")
    print("=" * 50)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
