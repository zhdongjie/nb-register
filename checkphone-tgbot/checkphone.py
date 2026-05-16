"""
Standalone GoPay phone availability checker for the Telegram bot.

This is intentionally copied out of gopay-app so the bot can run as an
independent container without calling the gopay-app gRPC service.
"""

import hashlib
import hmac
import json
import os
import random
import re
import time
import uuid
from typing import Optional
from urllib.parse import urlparse

import requests


GOPAY_COUNTRY_CODE = os.environ.get("GOPAY_COUNTRY_CODE", "62")
_GOPAY_PROXY_STATE_KEY = "_gopay_proxy"

GOTO_AUTH = "https://accounts.goto-products.com"
GOTO_CLIENT_ID = os.environ.get("GOTO_CLIENT_ID", "gopay:consumer:app")
GOTO_CLIENT_SECRET = os.environ.get("GOTO_CLIENT_SECRET", "")

HMAC_KEY = os.environ.get("GOPAY_HMAC_KEY", "")
DEFAULT_EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"
DEFAULT_X_E2 = os.environ.get("GOPAY_X_E2", "")
COMPACT_JSON_SEPARATORS = (",", ":")


class GopayProxyPoolExhausted(RuntimeError):
    pass


def gopay_proxy_pool_entries() -> list[str]:
    raw = os.environ.get("GOPAY_PROXY_POOL", "").strip()
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[\s,]+", raw) if item.strip()]


def _require_gopay_proxy_pool() -> list[str]:
    entries = gopay_proxy_pool_entries()
    if not entries:
        raise RuntimeError("GOPAY_PROXY_POOL is required")
    return entries


def _proxy_index(entries: list[str], proxy: str) -> int:
    try:
        return entries.index(str(proxy or "").strip())
    except ValueError:
        return -1


def _state_proxy_index(state: dict, entries: list[str]) -> int:
    if isinstance(state, dict):
        index = _proxy_index(entries, state.get(_GOPAY_PROXY_STATE_KEY, ""))
        if index >= 0:
            return index
    return -1


def gopay_proxy_for_attempt(attempt: int, state: dict = None) -> tuple[str, int, int]:
    entries = _require_gopay_proxy_pool()
    if attempt > len(entries):
        raise GopayProxyPoolExhausted("GOPAY_PROXY_POOL exhausted before login methods succeeded")
    current_index = _state_proxy_index(state, entries)
    if current_index < 0:
        index = 0
    elif attempt <= 1:
        index = current_index
    else:
        index = (current_index + 1) % len(entries)
    proxy = entries[index]
    if isinstance(state, dict):
        state[_GOPAY_PROXY_STATE_KEY] = proxy
    return proxy, index + 1, len(entries)


def gopay_proxy_attempt_limit() -> int:
    return max(1, len(gopay_proxy_pool_entries()))


def _random_d1() -> str:
    return ":".join(f"{b:02X}" for b in os.urandom(32))


def _random_widevine_id() -> str:
    import base64

    return base64.b64encode(hashlib.sha256(os.urandom(32)).digest()).decode()


def _random_appsflyer_id() -> str:
    return (
        f"{int(time.time() * 1000)}-"
        f"{random.randint(1000000000000000000, 9999999999999999999)}"
    )


def _random_wifi_mac() -> str:
    return "02:" + ":".join(f"{b:02x}" for b in os.urandom(5))


def _random_letters(length: int, alphabet: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ") -> str:
    return "".join(random.choice(alphabet) for _ in range(length))


def _random_brand_word() -> str:
    consonants = "bcdfghjklmnpqrstvwxyz"
    vowels = "aeiou"
    syllables = []
    for _ in range(random.randint(2, 4)):
        syllables.append(random.choice(consonants) + random.choice(vowels))
    if random.random() < 0.35:
        syllables.append(random.choice(("n", "r", "s", "x")))
    return "".join(syllables).capitalize()


def _random_phone_make() -> str:
    return _random_brand_word()


def _random_phone_model(make: str) -> str:
    prefix = "".join(ch for ch in make.upper() if ch.isalpha())[:2]
    if len(prefix) < 2:
        prefix = _random_letters(2)
    family = random.choice(("A", "C", "M", "N", "P", "R", "S", "V", "X", "Z"))
    number = random.randint(1000, 9999)
    suffix = _random_letters(random.randint(0, 2))
    separator = random.choice(("-", " "))
    return f"{make}, {prefix}{family}{separator}{number}{suffix}"


def _random_screen() -> str:
    width = random.randrange(720, 1448, 16)
    aspect = random.uniform(1.95, 2.28)
    height = int(width * aspect)
    height = min(3200, max(width + 640, (height // 8) * 8))
    screen = f"{width}x{height}"
    return "1088x2160" if screen == "1080x2148" else screen


def _random_android_version() -> str:
    return str(random.randint(10, 14))


def _random_device_shape() -> dict:
    make = _random_phone_make()
    return {
        "make": make,
        "model": _random_phone_model(make),
        "screen": _random_screen(),
        "android_version": _random_android_version(),
    }


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "")
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _use_env_identity() -> bool:
    return _env_flag("GOPAY_STATIC_DEVICE_IDENTITY")


def _identity_value(key: str, fallback, use_env_identity: bool) -> str:
    if use_env_identity:
        value = os.environ.get(key, "")
        if value:
            return value
    return fallback() if callable(fallback) else str(fallback)


def generate_device_fingerprint(randomize_model: Optional[bool] = None) -> dict:
    use_env_identity = _use_env_identity()
    shape = _random_device_shape()
    make, model = shape["make"], shape["model"]
    android_version = _identity_value("GOPAY_ANDROID_VERSION", shape["android_version"], use_env_identity)
    app_version = os.environ.get("GOPAY_APP_VERSION", "2.7.0")
    app_id = os.environ.get("GOPAY_APP_ID", "com.gojek.gopay")
    app_build = os.environ.get("GOPAY_APP_BUILD", "2070")
    d1 = os.environ.get("GOPAY_D1", "") if use_env_identity else ""
    if not d1 and os.environ.get("GOPAY_RANDOM_D1", "1") == "1":
        d1 = _random_d1()

    return {
        "x-apptype": "GOPAY",
        "x-appversion": app_version,
        "x-appid": app_id,
        "x-platform": "Android",
        "x-uniqueid": _identity_value("GOPAY_UNIQUE_ID", lambda: os.urandom(8).hex(), use_env_identity),
        "x-phonemake": make,
        "x-phonemodel": model,
        "x-deviceos": _identity_value("GOPAY_DEVICE_OS", f"Android, {android_version}", use_env_identity),
        "x-user-type": "customer",
        "x-session-id": _identity_value("GOPAY_SESSION_ID", lambda: str(uuid.uuid4()), use_env_identity),
        "transaction-id": _identity_value("GOPAY_TRANSACTION_ID", lambda: str(uuid.uuid4()), use_env_identity),
        "user-agent": _identity_value(
            "GOPAY_USER_AGENT",
            f"GoPay/{app_version} ({app_id}; build:{app_build}; Android, {android_version})",
            use_env_identity,
        ),
        "d1": d1,
        "x-e2": os.environ.get("GOPAY_X_E2", DEFAULT_X_E2),
        "adjts": os.environ.get("GOPAY_ADJTS", "host:D"),
        "m1_appsflyer_id": _identity_value("GOPAY_APPSFLYER_ID", _random_appsflyer_id, use_env_identity),
        "m1_widevine_id": _identity_value("GOPAY_WIDEVINE_ID", _random_widevine_id, use_env_identity),
        "m1_screen": _identity_value("GOPAY_SCREEN", shape["screen"], use_env_identity),
        "m1_wifi_mac": _identity_value("GOPAY_WIFI_MAC", _random_wifi_mac, use_env_identity),
        "m1_wifi_ssid": _random_wifi_ssid(),
        "m1_connection_id": str(random.randint(100000, 999999)),
        "m1_signature": _identity_value("GOPAY_M1_SIGNATURE", lambda: os.urandom(16).hex(), use_env_identity),
        "m1_device_uuid": str(uuid.uuid4()),
        "user-uuid": _identity_value("GOPAY_USER_UUID", "", use_env_identity),
        "x-devicetoken": _identity_value("GOPAY_DEVICE_TOKEN", "", use_env_identity),
        "x-location": os.environ.get("GOPAY_LOCATION", ""),
        "x-location-accuracy": os.environ.get("GOPAY_LOCATION_ACCURACY", ""),
        "gojek-country-code": os.environ.get("GOPAY_GOJEK_COUNTRY_CODE", "ID"),
    }


def _proxy_map(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    value = proxy.strip()
    if value.startswith("socks5://"):
        value = "socks5h://" + value[len("socks5://"):]
    return {"http": value, "https": value}


def _compact_json(body: Optional[dict]) -> str:
    if body is None:
        return ""
    return json.dumps(body, ensure_ascii=False, separators=COMPACT_JSON_SEPARATORS)


def _device_get(device: dict, key: str, default: str = "") -> str:
    return str(device.get(key) or device.get(key.lower()) or device.get(key.upper()) or default)


def _ensure_device_defaults(device: dict) -> dict:
    use_env_identity = _use_env_identity()
    shape = _random_device_shape()
    android_version = _identity_value("GOPAY_ANDROID_VERSION", shape["android_version"], use_env_identity)
    device.setdefault("x-apptype", "GOPAY")
    device.setdefault("x-appversion", os.environ.get("GOPAY_APP_VERSION", "2.7.0"))
    device.setdefault("x-appid", os.environ.get("GOPAY_APP_ID", "com.gojek.gopay"))
    device.setdefault("x-platform", "Android")
    device.setdefault("x-uniqueid", _identity_value("GOPAY_UNIQUE_ID", lambda: os.urandom(8).hex(), use_env_identity))
    device.setdefault("x-phonemake", shape["make"])
    device.setdefault("x-phonemodel", shape["model"])
    device.setdefault("x-deviceos", _identity_value("GOPAY_DEVICE_OS", f"Android, {android_version}", use_env_identity))
    device.setdefault("x-user-type", "customer")
    device.setdefault("x-session-id", _identity_value("GOPAY_SESSION_ID", lambda: str(uuid.uuid4()), use_env_identity))
    device.setdefault("transaction-id", _identity_value("GOPAY_TRANSACTION_ID", lambda: str(uuid.uuid4()), use_env_identity))
    device.setdefault(
        "user-agent",
        _identity_value(
            "GOPAY_USER_AGENT",
            f"GoPay/{device['x-appversion']} ({device['x-appid']}; build:{os.environ.get('GOPAY_APP_BUILD', '2070')}; Android, {android_version})",
            use_env_identity,
        ),
    )
    if not _device_get(device, "d1"):
        device["d1"] = _identity_value("GOPAY_D1", _random_d1, use_env_identity)
    device.setdefault("x-e2", os.environ.get("GOPAY_X_E2", DEFAULT_X_E2))
    device.setdefault("adjts", os.environ.get("GOPAY_ADJTS", "host:D"))
    device.setdefault("m1_appsflyer_id", _identity_value("GOPAY_APPSFLYER_ID", _random_appsflyer_id, use_env_identity))
    device.setdefault("m1_widevine_id", _identity_value("GOPAY_WIDEVINE_ID", _random_widevine_id, use_env_identity))
    device.setdefault("m1_screen", _identity_value("GOPAY_SCREEN", shape["screen"], use_env_identity))
    device.setdefault("m1_wifi_mac", _identity_value("GOPAY_WIFI_MAC", _random_wifi_mac, use_env_identity))
    device.setdefault("m1_wifi_ssid", _random_wifi_ssid())
    device.setdefault("m1_connection_id", str(random.randint(100000, 999999)))
    device.setdefault("m1_signature", _identity_value("GOPAY_M1_SIGNATURE", lambda: os.urandom(16).hex(), use_env_identity))
    device.setdefault("m1_device_uuid", str(uuid.uuid4()))
    device.setdefault("user-uuid", _identity_value("GOPAY_USER_UUID", "", use_env_identity))
    device.setdefault("x-devicetoken", _identity_value("GOPAY_DEVICE_TOKEN", "", use_env_identity))
    device.setdefault("x-location", os.environ.get("GOPAY_LOCATION", ""))
    device.setdefault("x-location-accuracy", os.environ.get("GOPAY_LOCATION_ACCURACY", ""))
    device.setdefault("gojek-country-code", os.environ.get("GOPAY_GOJEK_COUNTRY_CODE", "ID"))
    return device


def _current_x_m1(device: dict) -> str:
    return ",".join(
        [
            f"3:{_device_get(device, 'm1_appsflyer_id', _random_appsflyer_id())}",
            f"4:{_device_get(device, 'm1_connection_id', '5939')}",
            f"5:{_device_get(device, 'x-phonemake')}|3200|2",
            f"6:{_device_get(device, 'm1_wifi_mac', '02:00:00:00:00:00')}",
            f"7:{_device_get(device, 'm1_wifi_ssid', '<unknown ssid>')}",
            f"8:{_device_get(device, 'm1_screen', '1080x2148')}",
            "9:passive,network,fused,gps",
            "10:1",
            f"11:{_device_get(device, 'm1_widevine_id', _random_widevine_id())}",
            f"15:{_device_get(device, 'm1_signature')}",
            f"16:{_device_get(device, 'm1_device_uuid')}",
        ]
    )


def _security_host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _security_path(url: str) -> str:
    return urlparse(url).path


def generate_xe1(
    method: str,
    url: str,
    body: str,
    token: str,
    device: dict = None,
    x_m1: str = "",
) -> tuple:
    if device is None:
        device = {}
    _ensure_device_defaults(device)
    body_md5 = hashlib.md5(body.encode()).hexdigest() if body else DEFAULT_EMPTY_MD5
    override = os.environ.get("GOPAY_X_E1", "")
    if override:
        return override, body_md5
    if not HMAC_KEY:
        raise RuntimeError("GOPAY_HMAC_KEY is required to generate X-E1")

    field1 = os.urandom(32).hex() + "0" * 64 + os.urandom(16).hex()
    ts = str(int(time.time() * 1000))
    path = url.removeprefix("https://").removeprefix("http://")
    jwt = token.removeprefix("Bearer ")
    x_m1 = x_m1 or _current_x_m1(device)

    parts = [
        _device_get(device, "x-apptype", "GOPAY"),
        f"{_device_get(device, 'x-phonemodel')}:{jwt}",
        f"{_device_get(device, 'x-uniqueid')}:",
        f"{body_md5}:{path}",
        f"{method}:{ts}",
        f"{_device_get(device, 'x-deviceos')}:{_device_get(device, 'x-appversion')}",
        f"{x_m1}:{_device_get(device, 'x-appid')}",
        f"{field1}:{_device_get(device, 'x-phonemake')}",
        _device_get(device, "x-platform", "Android"),
    ]
    msg = ";".join(parts)
    first64 = hmac.new(HMAC_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()
    xe1 = f"{first64}:{field1}:{os.environ.get('GOPAY_X_E1_MARKER', 'D')}:{ts}"
    return xe1, body_md5


class GopayClient:
    def __init__(self, token: str, proxy: Optional[str] = None, device: Optional[dict] = None):
        self.token = token
        self.proxy = proxy
        self.device = _ensure_device_defaults(device or {})
        self.session = requests.Session()
        self.session.headers.clear()

    def _headers(self, method: str, url: str, body_str: str, extra_headers: Optional[dict]) -> dict:
        host = _security_host(url)
        x_m1 = _current_x_m1(self.device)
        xe1, body_md5 = generate_xe1(method, url, body_str, self.token, self.device, x_m1=x_m1)
        has_body = body_str != ""
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-CVSDK-Version": os.environ.get("GOPAY_CVSDK_VERSION", "1.0.0"),
            "Gojek-Service-Area": "1",
            "X-Request-ID": str(uuid.uuid1()),
            "Country-Code": _device_get(self.device, "gojek-country-code", "ID"),
            "X-AppVersion": _device_get(self.device, "x-appversion", "2.7.0"),
            "X-M1": x_m1,
            "Gojek-Country-Code": _device_get(self.device, "gojek-country-code", "ID"),
            "X-UniqueId": _device_get(self.device, "x-uniqueid"),
            "X-PhoneMake": _device_get(self.device, "x-phonemake", "Google"),
            "X-Help-Version": _device_get(self.device, "x-appversion", "2.7.0"),
            "X-E1": xe1,
            "User-Agent": _device_get(self.device, "user-agent"),
            "X-DeviceOS": _device_get(self.device, "x-deviceos", "Android, 11"),
            "X-User-Type": _device_get(self.device, "x-user-type", "customer"),
            "X-AppId": _device_get(self.device, "x-appid", "com.gojek.gopay"),
            "Gojek-Timezone": os.environ.get("GOPAY_TIMEZONE", "Asia/Jakarta"),
            "X-AuthSDK-Version": os.environ.get("GOPAY_AUTHSDK_VERSION", "1.0.0"),
            "X-AppType": _device_get(self.device, "x-apptype", "GOPAY"),
            "X-User-Locale": os.environ.get("GOPAY_USER_LOCALE", "en_ID"),
            "X-DeviceToken": _device_get(self.device, "x-devicetoken"),
            "X-E2": _device_get(self.device, "x-e2", DEFAULT_X_E2),
            "X-E3": body_md5,
            "Accept-Language": os.environ.get("GOPAY_ACCEPT_LANGUAGE", "en-ID"),
            "Transaction-ID": _device_get(self.device, "transaction-id"),
            "X-PhoneModel": _device_get(self.device, "x-phonemodel", "Google, sdk_gphone_arm64"),
            "X-Platform": _device_get(self.device, "x-platform", "Android"),
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}" if not self.token.startswith("Bearer") else self.token
        if _security_path(url).endswith("/api/v1/users/pin/tokens/nb"):
            headers["X-Biometric"] = ""
            headers["X-Verification"] = "pin"
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _request(self, method: str, url: str, body: Optional[dict] = None, extra_headers: Optional[dict] = None) -> dict:
        body_str = _compact_json(body)
        headers = self._headers(method, url, body_str, extra_headers)
        try:
            resp = self.session.request(
                method,
                url,
                data=body_str.encode() if body_str else None,
                headers=headers,
                proxies=_proxy_map(self.proxy),
                timeout=15,
            )
            try:
                payload = resp.json()
            except ValueError:
                payload = {"raw": resp.text}
            data = payload
            if isinstance(payload, dict) and "data" in payload and payload.get("data") is not None:
                data = payload["data"]
            return {"status": resp.status_code, "data": data, "raw": payload}
        except requests.RequestException as e:
            return {"status": 0, "data": {"error": str(e)}, "raw": {"error": str(e)}}

    def post(self, url: str, body: Optional[dict] = None, **kwargs) -> dict:
        return self._request("POST", url, body=body, **kwargs)

    def get(self, url: str, **kwargs) -> dict:
        return self._request("GET", url, **kwargs)


def _country_code(country_code: str = "") -> str:
    value = str(country_code or GOPAY_COUNTRY_CODE or "62").strip()
    return value if value.startswith("+") else f"+{value}"


def _normalize_phone(phone: str, country_code: str = "") -> str:
    prefix = _country_code(country_code).lstrip("+")
    value = str(phone or "").strip().lstrip("+")
    if value.startswith(prefix):
        value = value[len(prefix):]
    return value


def _auth_body(**extra) -> dict:
    body = dict(extra)
    body["client_id"] = GOTO_CLIENT_ID
    body["client_secret"] = GOTO_CLIENT_SECRET
    return body


def _response_error(label: str, response: dict) -> str:
    raw = response.get("raw")
    detail = raw if raw is not None else response.get("data")
    return f"{label}: status {response.get('status')} {detail}"


def _is_rate_limited(response: dict) -> bool:
    if response.get("status") == 429:
        return True
    raw = response.get("raw") if isinstance(response.get("raw"), dict) else {}
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    errors = raw.get("errors") or data.get("errors") or []
    for err in errors:
        if isinstance(err, dict) and "ratelimited" in str(err.get("code", "")).lower():
            return True
    return False


def login_methods_invalid_user(response: dict) -> bool:
    if response.get("status") != 401:
        return False
    raw = response.get("raw") if isinstance(response.get("raw"), dict) else {}
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    errors = raw.get("errors") or data.get("errors") or []
    for err in errors:
        if not isinstance(err, dict):
            continue
        text = " ".join(str(err.get(key, "")) for key in ("code", "message", "message_title")).lower()
        if "invalid user" in text or "could not find the user" in text:
            return True
    return False


def check_phone_by_login_methods(phone: str, country_code: str = "") -> dict:
    cc = _country_code(country_code)
    normalized_phone = _normalize_phone(phone, cc)
    attempts = gopay_proxy_attempt_limit()
    fingerprint_rotations = 0
    proxy_rotations = 0
    proxy_state = {}
    for attempt in range(1, attempts + 1):
        try:
            proxy, proxy_index, proxy_count = gopay_proxy_for_attempt(attempt, proxy_state)
        except GopayProxyPoolExhausted as exc:
            return {
                "success": False,
                "available": False,
                "status": "rate_limited",
                "error": str(exc),
                "attempts": attempt - 1,
                "fingerprint_rotations": fingerprint_rotations,
                "proxy_rotations": proxy_rotations,
                "proxy_pool_size": len(gopay_proxy_pool_entries()),
            }
        device = generate_device_fingerprint(randomize_model=True)
        c = GopayClient("", proxy=proxy, device=device)
        try:
            r = c.post(
                f"{GOTO_AUTH}/goto-auth/login/methods",
                body=_auth_body(
                    country_code=cc,
                    device_verification_token_id="",
                    email="",
                    phone_number=normalized_phone,
                ),
            )
        except Exception as e:
            return {
                "success": False,
                "available": False,
                "status": "error",
                "error": str(e),
                "attempts": attempt,
                "fingerprint_rotations": fingerprint_rotations,
                "proxy_rotations": proxy_rotations,
                "proxy_pool_size": proxy_count,
            }
        if r["status"] in (200, 201):
            data = r.get("data") if isinstance(r.get("data"), dict) else {}
            return {
                "success": True,
                "available": False,
                "status": "registered",
                "methods": data.get("methods") or [],
                "attempts": attempt,
                "fingerprint_rotations": fingerprint_rotations,
                "proxy_rotations": proxy_rotations,
                "proxy_pool_size": proxy_count,
            }
        if login_methods_invalid_user(r):
            return {
                "success": True,
                "available": True,
                "status": "available",
                "attempts": attempt,
                "fingerprint_rotations": fingerprint_rotations,
                "proxy_rotations": proxy_rotations,
                "proxy_pool_size": proxy_count,
            }
        if _is_rate_limited(r) and attempt < attempts:
            fingerprint_rotations += 1
            if proxy_count > 0:
                proxy_rotations += 1
            print(
                "[checkphone-tgbot] check phone rate limited; rotating fingerprint/proxy "
                f"attempt={attempt}/{attempts} proxy_index={proxy_index}/{proxy_count}",
                flush=True,
            )
            time.sleep(1)
            continue
        if _is_rate_limited(r):
            error = "GOPAY_PROXY_POOL exhausted before login methods succeeded"
            if attempt < proxy_count:
                error = _response_error("login methods rate limited", r)
            return {
                "success": False,
                "available": False,
                "status": "rate_limited",
                "error": error,
                "attempts": attempt,
                "fingerprint_rotations": fingerprint_rotations,
                "proxy_rotations": proxy_rotations,
                "proxy_pool_size": proxy_count,
            }
        return {
            "success": False,
            "available": False,
            "status": "error",
            "error": _response_error("login methods failed", r),
            "attempts": attempt,
            "fingerprint_rotations": fingerprint_rotations,
            "proxy_rotations": proxy_rotations,
            "proxy_pool_size": proxy_count,
        }
    return {
        "success": False,
        "available": False,
        "status": "error",
        "error": "login methods attempts exhausted",
    }
