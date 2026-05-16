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
import time
import uuid
from typing import Optional
from urllib.parse import urlparse

import requests


GOPAY_PROXY = os.environ.get("GOPAY_PROXY", os.environ.get("GOPAY_PROXY_URL", ""))
GOPAY_COUNTRY_CODE = os.environ.get("GOPAY_COUNTRY_CODE", "62")
GOPAY_LOGIN_FP_RETRIES = int(os.environ.get("GOPAY_LOGIN_FP_RETRIES", "8"))

GOTO_AUTH = "https://accounts.goto-products.com"
GOTO_CLIENT_ID = os.environ.get("GOTO_CLIENT_ID", "gopay:consumer:app")
GOTO_CLIENT_SECRET = os.environ.get("GOTO_CLIENT_SECRET", "")

HMAC_KEY = os.environ.get("GOPAY_HMAC_KEY", "")
DEFAULT_EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"
DEFAULT_X_E2 = os.environ.get("GOPAY_X_E2", "")
COMPACT_JSON_SEPARATORS = (",", ":")

PHONE_MODELS = [
    ("Samsung", "samsung, SM-G991B"),
    ("Samsung", "samsung, SM-A525F"),
    ("Samsung", "samsung, SM-S908B"),
    ("Xiaomi", "Xiaomi, M2101K6G"),
    ("Xiaomi", "Xiaomi, 22071219CG"),
    ("OPPO", "OPPO, CPH2211"),
    ("vivo", "vivo, V2111"),
    ("Google", "Google, sdk_gphone_arm64"),
    ("realme", "realme, RMX3085"),
    ("OnePlus", "OnePlus, LE2115"),
]


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


def generate_device_fingerprint(randomize_model: Optional[bool] = None) -> dict:
    if randomize_model is None:
        randomize_model = os.environ.get("GOPAY_RANDOM_DEVICE") == "1"
    if randomize_model:
        make, model = random.choice(PHONE_MODELS)
    else:
        make, model = ("Google", "Google, sdk_gphone_arm64")

    android_version = os.environ.get("GOPAY_ANDROID_VERSION", "11")
    app_version = os.environ.get("GOPAY_APP_VERSION", "2.7.0")
    app_id = os.environ.get("GOPAY_APP_ID", "com.gojek.gopay")
    app_build = os.environ.get("GOPAY_APP_BUILD", "2070")
    resolutions = ["1080x2400", "1080x2340", "1080x2148", "1080x1920", "720x1600"]
    d1 = os.environ.get("GOPAY_D1", "")
    if not d1 and os.environ.get("GOPAY_RANDOM_D1", "1") == "1":
        d1 = _random_d1()

    return {
        "x-apptype": "GOPAY",
        "x-appversion": app_version,
        "x-appid": app_id,
        "x-platform": "Android",
        "x-uniqueid": os.environ.get("GOPAY_UNIQUE_ID", os.urandom(8).hex()),
        "x-phonemake": make,
        "x-phonemodel": model,
        "x-deviceos": os.environ.get("GOPAY_DEVICE_OS", f"Android, {android_version}"),
        "x-user-type": "customer",
        "x-session-id": os.environ.get("GOPAY_SESSION_ID", str(uuid.uuid4())),
        "transaction-id": os.environ.get("GOPAY_TRANSACTION_ID", str(uuid.uuid4())),
        "user-agent": os.environ.get(
            "GOPAY_USER_AGENT",
            f"GoPay/{app_version} ({app_id}; build:{app_build}; Android, {android_version})",
        ),
        "d1": d1,
        "x-e2": os.environ.get("GOPAY_X_E2", DEFAULT_X_E2),
        "adjts": os.environ.get("GOPAY_ADJTS", "host:D"),
        "m1_appsflyer_id": os.environ.get("GOPAY_APPSFLYER_ID", _random_appsflyer_id()),
        "m1_widevine_id": os.environ.get("GOPAY_WIDEVINE_ID", _random_widevine_id()),
        "m1_screen": os.environ.get("GOPAY_SCREEN", random.choice(resolutions)),
        "m1_wifi_mac": os.environ.get("GOPAY_WIFI_MAC", "02:00:00:00:00:00"),
        "m1_signature": os.environ.get("GOPAY_M1_SIGNATURE", os.urandom(16).hex()),
        "user-uuid": os.environ.get("GOPAY_USER_UUID", ""),
        "x-devicetoken": os.environ.get("GOPAY_DEVICE_TOKEN", ""),
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
    device.setdefault("x-apptype", "GOPAY")
    device.setdefault("x-appversion", os.environ.get("GOPAY_APP_VERSION", "2.7.0"))
    device.setdefault("x-appid", os.environ.get("GOPAY_APP_ID", "com.gojek.gopay"))
    device.setdefault("x-platform", "Android")
    device.setdefault("x-uniqueid", os.environ.get("GOPAY_UNIQUE_ID", os.urandom(8).hex()))
    device.setdefault("x-phonemake", "Google")
    device.setdefault("x-phonemodel", "Google, sdk_gphone_arm64")
    device.setdefault("x-deviceos", os.environ.get("GOPAY_DEVICE_OS", "Android, 11"))
    device.setdefault("x-user-type", "customer")
    device.setdefault("x-session-id", os.environ.get("GOPAY_SESSION_ID", str(uuid.uuid4())))
    device.setdefault("transaction-id", os.environ.get("GOPAY_TRANSACTION_ID", str(uuid.uuid4())))
    device.setdefault(
        "user-agent",
        os.environ.get(
            "GOPAY_USER_AGENT",
            f"GoPay/{device['x-appversion']} ({device['x-appid']}; build:{os.environ.get('GOPAY_APP_BUILD', '2070')}; Android, 11)",
        ),
    )
    if not _device_get(device, "d1"):
        device["d1"] = os.environ.get("GOPAY_D1", "") or _random_d1()
    device.setdefault("x-e2", os.environ.get("GOPAY_X_E2", DEFAULT_X_E2))
    device.setdefault("adjts", os.environ.get("GOPAY_ADJTS", "host:D"))
    device.setdefault("m1_appsflyer_id", os.environ.get("GOPAY_APPSFLYER_ID", _random_appsflyer_id()))
    device.setdefault("m1_widevine_id", os.environ.get("GOPAY_WIDEVINE_ID", _random_widevine_id()))
    device.setdefault("m1_screen", os.environ.get("GOPAY_SCREEN", "1080x2148"))
    device.setdefault("m1_wifi_mac", os.environ.get("GOPAY_WIFI_MAC", "02:00:00:00:00:00"))
    device.setdefault("m1_signature", os.environ.get("GOPAY_M1_SIGNATURE", os.urandom(16).hex()))
    device.setdefault("user-uuid", os.environ.get("GOPAY_USER_UUID", ""))
    device.setdefault("x-devicetoken", os.environ.get("GOPAY_DEVICE_TOKEN", ""))
    device.setdefault("x-location", os.environ.get("GOPAY_LOCATION", ""))
    device.setdefault("x-location-accuracy", os.environ.get("GOPAY_LOCATION_ACCURACY", ""))
    device.setdefault("gojek-country-code", os.environ.get("GOPAY_GOJEK_COUNTRY_CODE", "ID"))
    return device


def _current_x_m1(device: dict) -> str:
    return ",".join(
        [
            f"3:{_device_get(device, 'm1_appsflyer_id', _random_appsflyer_id())}",
            "4:5939",
            "5:|0|4",
            f"6:{_device_get(device, 'm1_wifi_mac', '02:00:00:00:00:00')}",
            "7:<unknown ssid>",
            f"8:{_device_get(device, 'm1_screen', '1080x2148')}",
            "10:1",
            f"11:{_device_get(device, 'm1_widevine_id', _random_widevine_id())}",
            f"15:{_device_get(device, 'm1_signature')}",
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
    attempts = max(1, GOPAY_LOGIN_FP_RETRIES)
    for attempt in range(1, attempts + 1):
        device = generate_device_fingerprint(randomize_model=True)
        c = GopayClient("", proxy=GOPAY_PROXY, device=device)
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
            }
        if r["status"] in (200, 201):
            data = r.get("data") if isinstance(r.get("data"), dict) else {}
            return {
                "success": True,
                "available": False,
                "status": "registered",
                "methods": data.get("methods") or [],
            }
        if login_methods_invalid_user(r):
            return {"success": True, "available": True, "status": "available"}
        if _is_rate_limited(r) and attempt < attempts:
            time.sleep(1)
            continue
        if _is_rate_limited(r):
            return {
                "success": False,
                "available": False,
                "status": "rate_limited",
                "error": _response_error("login methods rate limited", r),
            }
        return {
            "success": False,
            "available": False,
            "status": "error",
            "error": _response_error("login methods failed", r),
        }
    return {
        "success": False,
        "available": False,
        "status": "error",
        "error": "login methods attempts exhausted",
    }
