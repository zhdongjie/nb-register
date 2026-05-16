"""
GoPay/Gojek mobile API request wrapper.

The 2026-05-13 captures in gopay-capture show the GoPay 2.7.0 request
shape: compact JSON bodies, stable device/session headers, D1/X-M1/X-E1/X-E2
security headers, X-E3 as the exact body MD5, and AdjTs=host:D.
"""

import base64
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


HMAC_KEY = os.environ.get("GOPAY_HMAC_KEY", "")
DEFAULT_EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"
DEFAULT_X_E2 = os.environ.get("GOPAY_X_E2", "")
COMPACT_JSON_SEPARATORS = (",", ":")
GOPAY_CUSTOMER_SLIM_GET_PATHS = {
    "/v1/users/profile",
    "/v1/payment-options/balances",
    "/v1/payment-options/profiles",
}
GOPAY_CUSTOMER_APP_HEADER_PATHS = {
    "/v1/qris/payments",
    "/v2/customer/payment-options/checkout/list",
    "/v1/customer/payment-options/settings/last-used",
    "/v1/promotions/evaluate",
    "/api/v1/users/pin/tokens",
    "/api/v1/users/pins/allowed",
    "/api/v2/users/pins/setup/tokens",
    "/cvs/v1/methods",
    "/cvs/v1/initiate",
    "/cvs/v1/verify",
}
GOJEK_ACTIVITY_PATHS = {
    "/v5/customers",
    "/v2/otp/retry",
    "/v5/customers/verificationUpdateProfile",
    "/gojek/v2/customer",
}
GOJEK_APP_HEADER_PATHS = {
    "/courier/v1/token",
}
GOPAY_CUSTOMER_LINKED_APP_PATH = "/v1/linkedapps"
GOPAY_CUSTOMER_LINK_PREFIX = "/v1/links/"

# 手机品牌/型号池
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
    return base64.b64encode(hashlib.sha256(os.urandom(32)).digest()).decode()


def _random_appsflyer_id() -> str:
    return (
        f"{int(time.time() * 1000)}-"
        f"{random.randint(1000000000000000000, 9999999999999999999)}"
    )


def generate_device_fingerprint(randomize_model: Optional[bool] = None) -> dict:
    """生成并持久化到 state 的设备/header 种子。"""
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


# 默认设备（可被覆盖）
DEVICE = generate_device_fingerprint()

GOTO_CLIENT_ID = os.environ.get("GOTO_CLIENT_ID", "gopay:consumer:app")
GOTO_CLIENT_SECRET = os.environ.get("GOTO_CLIENT_SECRET", "")
GOTO_SSO_CLIENT_ID = os.environ.get("GOTO_SSO_CLIENT_ID", "gojek:consumer:app")
GOTO_SSO_CLIENT_SECRET = os.environ.get("GOTO_SSO_CLIENT_SECRET", "")


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


def _is_gopay_customer_link_path(path: str) -> bool:
    return path == GOPAY_CUSTOMER_LINKED_APP_PATH or path.startswith(GOPAY_CUSTOMER_LINK_PREFIX)


def _is_gopay_customer_app_header_path(path: str) -> bool:
    if path in GOPAY_CUSTOMER_APP_HEADER_PATHS:
        return True
    if path == "/v1/festivals" or path.startswith("/v1/festivals/"):
        return True
    if path.startswith("/customers/v1/payments/"):
        return True
    if path.startswith("/v3/payments/") and path.endswith("/capture"):
        return True
    if path.startswith("/api/v2/challenges/") and path.endswith("/pin-page"):
        return True
    return False


def generate_xe1(
    method: str,
    url: str,
    body: str,
    token: str,
    device: dict = None,
    x_m1: str = "",
) -> tuple:
    """Generate X-E1 header. Returns (xe1, body_md5)."""
    if device is None:
        device = DEVICE
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
        self.device = _ensure_device_defaults(device or DEVICE)
        self.session = requests.Session()
        self.session.headers.clear()

    def new_fingerprint(self):
        """切换到新的随机设备指纹"""
        self.device = generate_device_fingerprint(randomize_model=True)
        return self.device

    def _headers(self, method: str, url: str, body_str: str, extra_headers: Optional[dict]) -> dict:
        host = _security_host(url)
        path = _security_path(url)
        x_m1 = _current_x_m1(self.device)
        xe1, body_md5 = generate_xe1(method, url, body_str, self.token, self.device, x_m1=x_m1)
        has_body = body_str != ""
        headers = {
            "X-AppVersion": _device_get(self.device, "x-appversion", "2.7.0"),
            "X-AppId": _device_get(self.device, "x-appid", "com.gojek.gopay"),
            "X-AppType": _device_get(self.device, "x-apptype", "GOPAY"),
            "Accept": "application/json",
            "User-Agent": _device_get(self.device, "user-agent"),
            "D1": _device_get(self.device, "d1"),
            "X-Session-ID": _device_get(self.device, "x-session-id"),
            "X-Platform": _device_get(self.device, "x-platform", "Android"),
            "X-UniqueId": _device_get(self.device, "x-uniqueid"),
            "X-User-Type": _device_get(self.device, "x-user-type", "customer"),
            "X-DeviceOS": _device_get(self.device, "x-deviceos", "Android, 11"),
            "X-PhoneMake": _device_get(self.device, "x-phonemake", "Google"),
            "X-PushTokenType": "FCM",
            "X-PhoneModel": _device_get(self.device, "x-phonemodel", "Google, sdk_gphone_arm64"),
            "Accept-Language": "en-ID",
            "X-User-Locale": "en_ID",
            "X-M1": x_m1,
            "X-E1": xe1,
            "X-E2": _device_get(self.device, "x-e2", DEFAULT_X_E2),
            "X-E3": body_md5,
            "AdjTs": _device_get(self.device, "adjts", "host:D"),
        }
        if has_body:
            headers["Content-Type"] = "application/json"

        def app_headers() -> dict:
            out = {
                "Accept-Encoding": "gzip",
                "Gojek-Service-Area": "1",
                "Country-Code": _device_get(self.device, "gojek-country-code", "ID"),
                "X-AppVersion": _device_get(self.device, "x-appversion", "2.7.0"),
                "X-M1": x_m1,
                "Gojek-Country-Code": _device_get(self.device, "gojek-country-code", "ID"),
                "X-UniqueId": _device_get(self.device, "x-uniqueid"),
                "X-PhoneMake": _device_get(self.device, "x-phonemake", "Google"),
                "X-Help-Version": _device_get(self.device, "x-appversion", "2.7.0"),
                "X-E1": xe1,
                "X-DeviceOS": _device_get(self.device, "x-deviceos", "Android, 11"),
                "X-User-Type": _device_get(self.device, "x-user-type", "customer"),
                "User-Agent": _device_get(self.device, "user-agent"),
                "X-AppId": _device_get(self.device, "x-appid", "com.gojek.gopay"),
                "Gojek-Timezone": os.environ.get("GOPAY_TIMEZONE", "Asia/Jakarta"),
                "X-AppType": _device_get(self.device, "x-apptype", "GOPAY"),
                "X-User-Locale": os.environ.get("GOPAY_USER_LOCALE", "en_ID"),
                "X-DeviceToken": _device_get(self.device, "x-devicetoken"),
                "X-E2": _device_get(self.device, "x-e2", DEFAULT_X_E2),
                "Accept-Language": os.environ.get("GOPAY_ACCEPT_LANGUAGE", "en-ID"),
                "X-PhoneModel": _device_get(self.device, "x-phonemodel", "Google, sdk_gphone_arm64"),
                "X-Platform": _device_get(self.device, "x-platform", "Android"),
            }
            if has_body:
                out["Content-Type"] = "application/json"
            return out

        if host == "accounts.goto-products.com":
            headers = {
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
                "Accept-Language": os.environ.get("GOPAY_ACCEPT_LANGUAGE", "en-ID"),
                "Transaction-ID": _device_get(self.device, "transaction-id"),
                "X-PhoneModel": _device_get(self.device, "x-phonemodel", "Google, sdk_gphone_arm64"),
                "X-Platform": _device_get(self.device, "x-platform", "Android"),
            }
            if has_body:
                headers["Content-Type"] = "application/json"
        elif host == "api.gojekapi.com" and (path in GOJEK_ACTIVITY_PATHS or path in GOJEK_APP_HEADER_PATHS):
            headers = app_headers()
        elif host == "customer.gopayapi.com" and _is_gopay_customer_link_path(path):
            headers = app_headers()
        elif host == "customer.gopayapi.com" and _is_gopay_customer_app_header_path(path):
            headers = app_headers()
        elif host == "customer.gopayapi.com" and method == "GET" and path in GOPAY_CUSTOMER_SLIM_GET_PATHS:
            headers = {
                "Accept-Encoding": "gzip",
                "X-AppVersion": _device_get(self.device, "x-appversion", "2.7.0"),
                "X-UniqueId": _device_get(self.device, "x-uniqueid"),
                "X-PhoneMake": _device_get(self.device, "x-phonemake", "Google"),
                "X-E1": xe1,
                "X-DeviceOS": _device_get(self.device, "x-deviceos", "Android, 11"),
                "X-User-Type": _device_get(self.device, "x-user-type", "customer"),
                "User-Agent": _device_get(self.device, "user-agent"),
                "X-AppId": _device_get(self.device, "x-appid", "com.gojek.gopay"),
                "X-AppType": _device_get(self.device, "x-apptype", "GOPAY"),
                "X-E2": _device_get(self.device, "x-e2", DEFAULT_X_E2),
                "X-M1": x_m1,
                "X-PhoneModel": _device_get(self.device, "x-phonemodel", "Google, sdk_gphone_arm64"),
                "X-Platform": _device_get(self.device, "x-platform", "Android"),
                "Accept-Language": os.environ.get("GOPAY_ACCEPT_LANGUAGE", "en-ID"),
                "X-User-Locale": os.environ.get("GOPAY_USER_LOCALE", "en_ID"),
                "X-DeviceToken": _device_get(self.device, "x-devicetoken"),
                "Gojek-Country-Code": _device_get(self.device, "gojek-country-code", "ID"),
            }
        else:
            headers.update(
                {
                    "User-uuid": _device_get(self.device, "user-uuid"),
                    "X-DeviceToken": _device_get(self.device, "x-devicetoken"),
                    "X-Location": _device_get(self.device, "x-location"),
                    "X-Location-Accuracy": _device_get(self.device, "x-location-accuracy"),
                    "Gojek-Country-Code": _device_get(self.device, "gojek-country-code", "ID"),
                    "X-Dark-Mode": "false",
                }
            )
        if path.endswith("/api/v1/users/pin/tokens/nb"):
            headers["X-Biometric"] = ""
            headers["X-Verification"] = "pin"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}" if not self.token.startswith("Bearer") else self.token
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
            return {"status": 0, "data": {"error": str(e)}}

    def get(self, url: str, **kwargs) -> dict:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, body: Optional[dict] = None, **kwargs) -> dict:
        return self._request("POST", url, body=body, **kwargs)

    def patch(self, url: str, body: Optional[dict] = None, **kwargs) -> dict:
        return self._request("PATCH", url, body=body, **kwargs)

    def put(self, url: str, body: Optional[dict] = None, **kwargs) -> dict:
        return self._request("PUT", url, body=body, **kwargs)

    def delete(self, url: str, body: Optional[dict] = None, **kwargs) -> dict:
        return self._request("DELETE", url, body=body, **kwargs)
