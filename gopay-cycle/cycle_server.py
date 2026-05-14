"""
GoPay Cycle gRPC Service - 显式步骤模式
外部编排服务按需调用改号、注销、登录等步骤；本服务不做后台自动循环。
"""

import contextvars
import json
import os
import time
import uuid
from concurrent import futures

import grpc
import gopay_cycle_pb2
import gopay_cycle_pb2_grpc

from gopay_client import GopayClient, generate_device_fingerprint
from gopay_cycle import (
    GOPAY_MIN_BALANCE_RP,
    GOPAY_PIN,
    PROXY,
    access_token_expires_at,
    access_token_usable,
    complete_login,
    complete_signup,
    complete_signup_pin,
    ensure_access_token,
    check_token_valid,
    check_phone_by_login_methods,
    expire_login_if_needed,
    expire_signup_if_needed,
    clear_tmp_tokens,
    migrate_active_tokens_to_tmp,
    start_login,
    start_signup,
    start_signup_pin,
)

PORT = int(os.environ.get("CYCLE_PORT", "50051"))
MAIN_PHONE = os.environ.get("GOPAY_PHONE", "")
GOPAY_COUNTRY_CODE = os.environ.get("GOPAY_COUNTRY_CODE", "62").strip() or "62"
GOPAY_SIGNUP_NAME = os.environ.get("GOPAY_SIGNUP_NAME", "gg").strip()
GOPAY_SIGNUP_EMAIL = os.environ.get("GOPAY_SIGNUP_EMAIL", "gg@example.com").strip()

GOPAY_API = "https://customer.gopayapi.com"
GOPAY_CUSTOMER = GOPAY_API
GOJEK_API = "https://api.gojekapi.com"
GOTO_AUTH = "https://accounts.goto-products.com"
GOTO_SSO_CLIENT_ID = os.environ.get("GOTO_SSO_CLIENT_ID", "gojek:consumer:app")
GOTO_SSO_CLIENT_SECRET = os.environ.get("GOTO_SSO_CLIENT_SECRET", "")
GOPAY_CHANGE_PHONE_COUNTRY_SYNC = os.environ.get("GOPAY_CHANGE_PHONE_COUNTRY_SYNC", "").strip().lower() in {"1", "true", "yes", "on"}
_CURRENT_STATE = contextvars.ContextVar("gopay_cycle_state", default=None)


def _parse_state_json(raw: str) -> dict:
    raw = str(raw or "").strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("state_json must be a JSON object")
    return value


def _state_json(state: dict) -> str:
    return json.dumps(state or {}, ensure_ascii=False, separators=(",", ":"))


def load_state():
    state = _CURRENT_STATE.get()
    return state if state is not None else {}


def save_state(state):
    # Stateless service: callers persist the returned state_json via DB CRUD.
    return None


def _state_from_request(request) -> dict:
    raw = getattr(request, "state_json", "")
    if raw:
        return _parse_state_json(raw)
    current = _CURRENT_STATE.get()
    return current if current is not None else {}


def _stateful_rpc(fn):
    def wrapped(self, request, context):
        state = _state_from_request(request)
        token = _CURRENT_STATE.set(state)
        try:
            resp = fn(self, request, context)
            if hasattr(resp, "state_json"):
                resp.state_json = _state_json(_CURRENT_STATE.get() or state)
            return resp
        finally:
            _CURRENT_STATE.reset(token)
    return wrapped


def _client(state) -> GopayClient:
    refresh = ensure_access_token(state)
    if not refresh.get("success") and not access_token_usable(state, 0):
        raise RuntimeError(refresh.get("error", "token refresh failed"))
    return GopayClient(state.get("token", ""), proxy=PROXY, device=state.get("device"))


def _phone_country_code(explicit: str = "") -> str:
    value = str(explicit or "").strip()
    if value:
        return value if value.startswith("+") else f"+{value}"
    return f"+{GOPAY_COUNTRY_CODE.lstrip('+')}"


def _has_cycle_seed(state) -> bool:
    return access_token_usable(state, 30)


def _tmp_access_token_usable(state: dict, min_ttl_seconds: int = 30) -> bool:
    token = str(state.get("_tmp_token", "")).strip()
    if not token:
        return False
    expires_at = access_token_expires_at(token) or int(state.get("_tmp_token_expires_at") or 0)
    if not expires_at:
        return True
    return expires_at > int(time.time()) + min_ttl_seconds


def _has_tmp_cycle_seed(state: dict) -> bool:
    return _tmp_access_token_usable(state, 30)


def _tmp_client(state: dict) -> GopayClient:
    token = str(state.get("_tmp_token", "")).strip()
    if not token:
        raise RuntimeError("tmp cycle token missing")
    if not _tmp_access_token_usable(state, 0):
        expires_at = access_token_expires_at(token) or int(state.get("_tmp_token_expires_at") or 0)
        raise RuntimeError(f"tmp cycle token expired: expires_at={expires_at}")
    return GopayClient(token, proxy=PROXY, device=state.get("device"))


def _pin(request_pin: str = "") -> str:
    return str(request_pin or GOPAY_PIN or "").strip()


def _signup_name() -> str:
    return GOPAY_SIGNUP_NAME or "gg"


def _signup_email() -> str:
    return GOPAY_SIGNUP_EMAIL or "gg@example.com"


def _change_phone_profile_body(state: dict, country_code: str, normalized_phone: str) -> dict:
    changed = False
    name = str(state.get("name") or "").strip()
    if not name:
        name = "gg"
        state["name"] = name
        changed = True
    email = str(state.get("email") or "").strip()
    if not email:
        email = f"gg{uuid.uuid4().hex[:12]}@aa.cc"
        state["email"] = email
        changed = True
    if changed:
        save_state(state)
    return {
        "email": email,
        "name": name,
        "phone": f"{country_code}{normalized_phone}",
        "profile_image_url": None,
    }


def _api_error(label: str, response: dict) -> str:
    status = response.get("status")
    if status == 401:
        return "AUTH_INVALID"
    detail = response.get("raw") or response.get("data")
    return f"{label}: status {status} {detail}"


def _response_errors(response: dict) -> list:
    raw = response.get("raw") if isinstance(response.get("raw"), dict) else {}
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    errors = raw.get("errors") or data.get("errors") or []
    return errors if isinstance(errors, list) else []


def _response_text(response: dict) -> str:
    return str(response.get("raw") or response.get("data") or "")


def _deactivation_otp_required(response: dict) -> bool:
    if response.get("status") == 462:
        return True
    for err in _response_errors(response):
        if isinstance(err, dict) and str(err.get("code", "")).upper() == "GOPAY-1603":
            return True
    return False


def _deactivation_check_ready(response: dict) -> bool:
    return response.get("status") == 200 or _deactivation_otp_required(response)


def _phone_registered_response(response: dict) -> bool:
    if response.get("status") != 400:
        return False
    text = _response_text(response).lower()
    return "user_can_not_update_phone" in text or "already registered" in text


def _token_check_ready(result: dict) -> bool:
    return bool(result.get("success") and result.get("token_valid") and result.get("has_min_balance"))


def _token_check_valid(result: dict) -> bool:
    return bool(result.get("token_valid"))


def _token_check_error(result: dict) -> str:
    if result.get("error"):
        return str(result.get("error"))
    amount = int(result.get("balance_amount") or 0)
    currency = str(result.get("balance_currency") or "IDR")
    return f"insufficient gopay balance: {amount} {currency} < {GOPAY_MIN_BALANCE_RP} IDR"


def _complete_login(otp: str):
    """完成 GoPay 登录（收到主号 OTP 后调用）"""
    state = load_state()
    complete_login(state, otp)
    print(f"  Logged in! Token ready.")


class GopayCycleServicer(gopay_cycle_pb2_grpc.GopayCycleServiceServicer):

    def CheckPhone(self, request, context):
        try:
            country_code = _phone_country_code(request.country_code)
            prefix = country_code.lstrip("+")
            normalized_phone = str(request.phone or "").strip().lstrip("+")
            if normalized_phone.startswith(prefix):
                normalized_phone = normalized_phone[len(prefix):]
            if not normalized_phone:
                return gopay_cycle_pb2.CheckPhoneResponse(
                    available=False, status="error", error_message="phone required")

            result = check_phone_by_login_methods(normalized_phone, country_code)
            if result.get("success") and result.get("available"):
                return gopay_cycle_pb2.CheckPhoneResponse(available=True, status="available")
            if result.get("success") and result.get("status") == "registered":
                return gopay_cycle_pb2.CheckPhoneResponse(
                    available=False, status="registered", error_message="PHONE_REGISTERED")
            if result.get("status") == "rate_limited":
                return gopay_cycle_pb2.CheckPhoneResponse(
                    available=False, status="rate_limited", error_message=str(result.get("error") or "RATE_LIMITED"))
            return gopay_cycle_pb2.CheckPhoneResponse(
                available=False, status="error", error_message=str(result.get("error") or "check phone failed"))
        except Exception as e:
            return gopay_cycle_pb2.CheckPhoneResponse(
                available=False, status="error", error_message=str(e))

    def ChangePhoneStart(self, request, context):
        try:
            state = load_state()
            pin = _pin(request.pin)
            phone = str(request.new_phone or "").strip()
            country_code = _phone_country_code()
            prefix = country_code.lstrip("+")
            normalized_phone = phone.lstrip("+")
            if normalized_phone.startswith(prefix):
                normalized_phone = normalized_phone[len(prefix):]

            if not _has_cycle_seed(state):
                return gopay_cycle_pb2.ChangePhoneStartResponse(
                    success=False, error_message="cycle seed token missing")
            if not pin:
                return gopay_cycle_pb2.ChangePhoneStartResponse(
                    success=False, error_message="gopay pin missing")
            if not normalized_phone:
                return gopay_cycle_pb2.ChangePhoneStartResponse(
                    success=False, error_message="new_phone required")

            usage_key = f"_temp_phone_usage_{normalized_phone}"
            if state.get(usage_key, 0) >= 2:
                return gopay_cycle_pb2.ChangePhoneStartResponse(
                    success=False, error_message="PHONE_EXHAUSTED")

            device = state.get("device") or generate_device_fingerprint()
            state["device"] = device
            c = _client(state)
            body = _change_phone_profile_body(state, country_code, normalized_phone)

            checked = (
                state.get("_checked_change_phone") == normalized_phone
                and state.get("_checked_change_phone_status") == "available"
            )
            if not checked:
                r = c.patch(f"{GOJEK_API}/v5/customers", body=body)
                if _phone_registered_response(r):
                    return gopay_cycle_pb2.ChangePhoneStartResponse(
                        success=False, error_message="PHONE_REGISTERED")
                if r["status"] != 461:
                    return gopay_cycle_pb2.ChangePhoneStartResponse(
                        success=False, error_message=_api_error("pin challenge failed", r))

            r = c.patch(f"{GOJEK_API}/v5/customers", body=body, extra_headers={"pin": pin})
            if _phone_registered_response(r):
                return gopay_cycle_pb2.ChangePhoneStartResponse(
                    success=False, error_message="PHONE_REGISTERED")
            if r["status"] != 200:
                return gopay_cycle_pb2.ChangePhoneStartResponse(
                    success=False, error_message=_api_error("pin submit failed", r))

            data = r.get("data") or {}
            otp_token = data.get("otp_token", "")
            if not otp_token:
                return gopay_cycle_pb2.ChangePhoneStartResponse(
                    success=False, error_message="otp_token missing")

            state["_change_phone"] = normalized_phone
            state["_change_otp_token"] = otp_token
            state.pop("_checked_change_phone", None)
            state.pop("_checked_change_phone_status", None)
            now = int(time.time())
            state["_change_otp_sent_at"] = now
            state["_change_otp_expires_at"] = now + int(data.get("expires_in") or 300)
            state["stage"] = "change_phone_otp_pending"
            state.pop("last_error", None)
            save_state(state)
            return gopay_cycle_pb2.ChangePhoneStartResponse(
                success=True, new_phone=normalized_phone, otp_sent=True)
        except Exception as e:
            return gopay_cycle_pb2.ChangePhoneStartResponse(success=False, error_message=str(e))

    def ChangePhoneRetry(self, request, context):
        try:
            state = load_state()
            otp_token = state.get("_change_otp_token", "")
            phone = state.get("_change_phone", "")
            if not otp_token or not phone:
                return gopay_cycle_pb2.ChangePhoneRetryResponse(
                    success=False, error_message=f"not waiting for change phone otp: {state.get('stage', 'idle')}")

            c = _client(state)
            r = c.post(f"{GOJEK_API}/v2/otp/retry", body={
                "otp_token": otp_token,
                "channel_type": "sms",
            })
            if r["status"] != 200:
                return gopay_cycle_pb2.ChangePhoneRetryResponse(
                    success=False, error_message=_api_error("otp retry failed", r))

            data = r.get("data") or {}
            new_otp_token = data.get("otp_token", "")
            if not new_otp_token:
                return gopay_cycle_pb2.ChangePhoneRetryResponse(
                    success=False, error_message="retry otp_token missing")

            now = int(time.time())
            state["_change_otp_token"] = new_otp_token
            state["_change_otp_sent_at"] = now
            state["_change_otp_expires_at"] = now + int(data.get("otp_expires_in") or 300)
            state["stage"] = "change_phone_otp_pending"
            state.pop("last_error", None)
            save_state(state)
            return gopay_cycle_pb2.ChangePhoneRetryResponse(success=True, otp_sent=True)
        except Exception as e:
            return gopay_cycle_pb2.ChangePhoneRetryResponse(success=False, error_message=str(e))

    def ChangePhoneComplete(self, request, context):
        try:
            state = load_state()
            otp_token = state.get("_change_otp_token", "")
            phone = state.get("_change_phone", "")
            if not otp_token or not phone:
                return gopay_cycle_pb2.ChangePhoneCompleteResponse(
                    success=False, error_message=f"not waiting for change phone otp: {state.get('stage', 'idle')}")
            if not request.otp:
                return gopay_cycle_pb2.ChangePhoneCompleteResponse(
                    success=False, error_message="otp required")

            c = _client(state)
            r = c.post(f"{GOJEK_API}/v5/customers/verificationUpdateProfile",
                       body={"otp": request.otp, "otp_token": otp_token})
            if r["status"] != 200:
                return gopay_cycle_pb2.ChangePhoneCompleteResponse(
                    success=False, error_message=_api_error("otp verify failed", r))

            if GOPAY_CHANGE_PHONE_COUNTRY_SYNC:
                r = c.put(f"{GOPAY_API}/customers/v1/country-change")
                if r["status"] != 200:
                    return gopay_cycle_pb2.ChangePhoneCompleteResponse(
                        success=False, error_message=_api_error("country-change sync failed", r))

            state["phone"] = phone
            usage_key = f"_temp_phone_usage_{phone}"
            state[usage_key] = state.get(usage_key, 0) + 1
            migrate_active_tokens_to_tmp(state, phone=phone)
            state["stage"] = "phone_changed"
            state.pop("last_error", None)
            state.pop("_change_phone", None)
            state.pop("_change_otp_token", None)
            state.pop("_change_otp_sent_at", None)
            state.pop("_change_otp_expires_at", None)
            save_state(state)
            return gopay_cycle_pb2.ChangePhoneCompleteResponse(success=True)
        except Exception as e:
            return gopay_cycle_pb2.ChangePhoneCompleteResponse(success=False, error_message=str(e))

    def DeactivateStart(self, request, context):
        try:
            state = load_state()
            pin = _pin(request.pin)
            if not _has_tmp_cycle_seed(state):
                return gopay_cycle_pb2.DeactivateStartResponse(
                    success=False, error_message="tmp cycle token missing")

            c = _tmp_client(state)
            profile = c.get(f"{GOPAY_CUSTOMER}/v1/users/profile")
            pin_setup = False
            if profile["status"] == 200:
                pin_setup = bool((profile.get("data") or {}).get("is_pin_setup"))
            elif pin:
                pin_setup = True

            if pin_setup:
                if not pin:
                    return gopay_cycle_pb2.DeactivateStartResponse(
                        success=False, error_message="gopay pin missing")
                r = c.post(f"{GOPAY_CUSTOMER}/api/v1/users/pin/challenges", body={"flow": "deactivation"})
                if r["status"] != 200:
                    return gopay_cycle_pb2.DeactivateStartResponse(
                        success=False, error_message=_api_error("deactivation challenge failed", r))

                challenge_id = r["data"].get("challenge_id", "")
                client_id = r["data"].get("client_id", "")
                if not challenge_id or not client_id:
                    return gopay_cycle_pb2.DeactivateStartResponse(
                        success=False, error_message="deactivation challenge missing id")

                r = c.post(f"{GOPAY_CUSTOMER}/api/v1/users/pin/tokens",
                           body={"challenge_id": challenge_id, "client_id": client_id, "pin": pin})
                if r["status"] != 200:
                    return gopay_cycle_pb2.DeactivateStartResponse(
                        success=False, error_message=_api_error("deactivation pin verify failed", r))

            r = c.get(f"{GOPAY_API}/api/v1/users/deactivate/check")
            if not _deactivation_check_ready(r):
                return gopay_cycle_pb2.DeactivateStartResponse(
                    success=False, error_message=_api_error("deactivation check failed", r))

            state["stage"] = "deactivate_otp_pending"
            state.pop("last_error", None)
            save_state(state)
            return gopay_cycle_pb2.DeactivateStartResponse(success=True, otp_sent=True)
        except Exception as e:
            return gopay_cycle_pb2.DeactivateStartResponse(success=False, error_message=str(e))

    def DeactivateComplete(self, request, context):
        try:
            state = load_state()
            if not request.otp:
                return gopay_cycle_pb2.DeactivateCompleteResponse(
                    success=False, error_message="otp required")

            c = _tmp_client(state)
            r = c.delete(f"{GOPAY_CUSTOMER}/api/v1/users/deactivate",
                         body={
                             "otp": request.otp,
                             "reason": "I no longer need digital payment services",
                             "description": None,
                         })
            if r["status"] != 200:
                return gopay_cycle_pb2.DeactivateCompleteResponse(
                    success=False, error_message=_api_error("deactivate failed", r))

            deactivated_at = int(time.time())
            state["deactivated_at"] = deactivated_at
            state["stage"] = "deactivated"
            state.pop("last_error", None)
            clear_tmp_tokens(state)
            save_state(state)
            return gopay_cycle_pb2.DeactivateCompleteResponse(success=True, deactivated_at=deactivated_at)
        except Exception as e:
            return gopay_cycle_pb2.DeactivateCompleteResponse(success=False, error_message=str(e))

    def LoginStart(self, request, context):
        try:
            state = load_state()
            if expire_login_if_needed(state):
                save_state(state)
            stage = str(state.get("stage", "")).strip()
            if stage in ("ready", "consumed"):
                token_check = check_token_valid(state)
                state = load_state()
                if _token_check_ready(token_check):
                    return gopay_cycle_pb2.LoginStartResponse(success=True, otp_sent=False)
                if token_check.get("token_valid"):
                    return gopay_cycle_pb2.LoginStartResponse(
                        success=False,
                        error_message=_token_check_error(token_check),
                    )
                print(f"[gopay-cycle] ready token validation failed; falling back to GoPay login: {token_check.get('error', '')}")

            phone = str(request.phone or state.get("phone") or MAIN_PHONE or "").strip()
            if not phone and stage in ("login", "login_otp_pending"):
                phone = str(state.get("_login_phone", "")).strip()
            if not phone:
                return gopay_cycle_pb2.LoginStartResponse(
                    success=False, error_message="login phone missing")
            country_code = _phone_country_code()
            prefix = country_code.lstrip("+")
            normalized_phone = phone.lstrip("+")
            if normalized_phone.startswith(prefix):
                normalized_phone = normalized_phone[len(prefix):]

            if stage == "login_otp_pending" and state.get("_login_otp_token") and state.get("_login_2fa_token"):
                return gopay_cycle_pb2.LoginStartResponse(
                    success=True,
                    otp_sent=True,
                    verification_id=state.get("_login_verification_id", ""),
                )

            pin = _pin("")
            result = start_login(state, normalized_phone, pin, country_code)
            if not result.get("success"):
                return gopay_cycle_pb2.LoginStartResponse(
                    success=False, error_message=result.get("error", "login start failed"))
            if result.get("ready"):
                state = load_state()
                token_check = check_token_valid(state)
                state = load_state()
                if not _token_check_ready(token_check):
                    return gopay_cycle_pb2.LoginStartResponse(
                        success=False,
                        error_message=_token_check_error(token_check),
                    )
            return gopay_cycle_pb2.LoginStartResponse(
                success=True,
                otp_sent=bool(result.get("otp_sent")),
                verification_id=result.get("verification_id", ""),
            )
        except Exception as e:
            return gopay_cycle_pb2.LoginStartResponse(success=False, error_message=str(e))

    def LoginComplete(self, request, context):
        """提交主号 OTP 完成 GoPay 登录"""
        try:
            state = load_state()
            if expire_login_if_needed(state):
                save_state(state)
            if state.get("stage") != "login_otp_pending":
                return gopay_cycle_pb2.LoginCompleteResponse(
                    success=False, error_message=f"not waiting for login otp: {state.get('stage', 'idle')}")
            _complete_login(request.otp)
            state = load_state()
            return gopay_cycle_pb2.LoginCompleteResponse(
                success=True, phone=state.get("phone", ""))
        except Exception as e:
            return gopay_cycle_pb2.LoginCompleteResponse(success=False, error_message=str(e))

    def SignupStart(self, request, context):
        try:
            state = load_state()
            changed = expire_login_if_needed(state) or expire_signup_if_needed(state)
            if changed:
                save_state(state)
            phone = str(MAIN_PHONE or "").strip()
            name = str(request.name or GOPAY_SIGNUP_NAME or "").strip()
            email = str(request.email or GOPAY_SIGNUP_EMAIL or "").strip()
            country_code = _phone_country_code(request.country_code)
            result = start_signup(state, phone, name, email, country_code)
            if not result.get("success"):
                return gopay_cycle_pb2.SignupStartResponse(
                    success=False,
                    error_message=result.get("error", "signup start failed"),
                )
            return gopay_cycle_pb2.SignupStartResponse(
                success=True,
                otp_sent=bool(result.get("otp_sent")),
                verification_id=result.get("verification_id", ""),
                verification_method=result.get("method", ""),
            )
        except Exception as e:
            return gopay_cycle_pb2.SignupStartResponse(success=False, error_message=str(e))

    def SignupComplete(self, request, context):
        try:
            state = load_state()
            if expire_signup_if_needed(state):
                save_state(state)
            result = complete_signup(state, request.otp)
            if not result.get("success"):
                return gopay_cycle_pb2.SignupCompleteResponse(
                    success=False,
                    error_message=result.get("error", "signup complete failed"),
                )
            return gopay_cycle_pb2.SignupCompleteResponse(
                success=True,
                phone=result.get("phone", ""),
                pin_setup_required=bool(result.get("pin_setup_required")),
            )
        except Exception as e:
            return gopay_cycle_pb2.SignupCompleteResponse(success=False, error_message=str(e))

    def CreatePinStart(self, request, context):
        try:
            state = load_state()
            if expire_signup_if_needed(state):
                save_state(state)
            result = start_signup_pin(state, _pin(request.pin))
            if not result.get("success"):
                return gopay_cycle_pb2.CreatePinStartResponse(
                    success=False,
                    error_message=result.get("error", "create pin start failed"),
                )
            return gopay_cycle_pb2.CreatePinStartResponse(
                success=True,
                otp_sent=bool(result.get("otp_sent")),
                verification_id=result.get("verification_id", ""),
                verification_method=result.get("method", ""),
            )
        except Exception as e:
            return gopay_cycle_pb2.CreatePinStartResponse(success=False, error_message=str(e))

    def CreatePinComplete(self, request, context):
        try:
            state = load_state()
            if expire_signup_if_needed(state):
                save_state(state)
            result = complete_signup_pin(state, request.otp, _pin(request.pin))
            if not result.get("success"):
                return gopay_cycle_pb2.CreatePinCompleteResponse(
                    success=False,
                    error_message=result.get("error", "create pin complete failed"),
                )
            return gopay_cycle_pb2.CreatePinCompleteResponse(
                success=True,
                phone=result.get("phone", ""),
                pin_setup_complete=bool(result.get("pin_setup_complete")),
            )
        except Exception as e:
            return gopay_cycle_pb2.CreatePinCompleteResponse(success=False, error_message=str(e))

    def AuthStart(self, request, context):
        try:
            state = load_state()
            changed = expire_login_if_needed(state) or expire_signup_if_needed(state)
            if changed:
                save_state(state)
                state = load_state()

            token_check = check_token_valid(state)
            state = load_state()
            if _token_check_valid(token_check):
                return gopay_cycle_pb2.AuthStartResponse(
                    success=True,
                    mode="token",
                    stage=state.get("stage", "ready"),
                    ready=True,
                )
            stage = str(state.get("stage", "")).strip()
            if stage == "login_otp_pending":
                return gopay_cycle_pb2.AuthStartResponse(
                    success=True,
                    mode="login",
                    stage=stage,
                    otp_sent=True,
                    verification_id=state.get("_login_verification_id", ""),
                    verification_method=state.get("_login_verification_method", ""),
                )
            if stage == "signup_pin_otp_pending":
                return gopay_cycle_pb2.AuthStartResponse(
                    success=True,
                    mode="signup",
                    stage=stage,
                    otp_sent=True,
                    verification_id=state.get("_signup_pin_verification_id", ""),
                    verification_method=state.get("_signup_pin_verification_method", ""),
                    pin_setup_required=True,
                )
            if stage == "signup_pin_required":
                resp = self.CreatePinStart(
                    gopay_cycle_pb2.CreatePinStartRequest(pin=request.pin),
                    context,
                )
                state = load_state()
                return gopay_cycle_pb2.AuthStartResponse(
                    success=resp.success,
                    error_message=resp.error_message,
                    mode="signup",
                    stage=state.get("stage", "idle"),
                    otp_sent=resp.otp_sent,
                    verification_id=resp.verification_id,
                    verification_method=resp.verification_method,
                    pin_setup_required=True,
                )
            if stage == "signup_otp_pending":
                return gopay_cycle_pb2.AuthStartResponse(
                    success=True,
                    mode="signup",
                    stage=stage,
                    otp_sent=True,
                    verification_id=state.get("_signup_verification_id", ""),
                    verification_method=state.get("_signup_verification_method", ""),
                )

            phone = str(MAIN_PHONE or "").strip()
            if not phone:
                return gopay_cycle_pb2.AuthStartResponse(success=False, error_message="GOPAY_PHONE missing")
            country_code = _phone_country_code(request.country_code)
            prefix = country_code.lstrip("+")
            normalized_phone = phone.lstrip("+")
            if normalized_phone.startswith(prefix):
                normalized_phone = normalized_phone[len(prefix):]

            result = start_login(state, normalized_phone, _pin(request.pin), country_code)
            if result.get("success"):
                state = load_state()
                ready = bool(result.get("ready"))
                if ready:
                    token_check = check_token_valid(state)
                    state = load_state()
                    if not _token_check_valid(token_check):
                        return gopay_cycle_pb2.AuthStartResponse(
                            success=False,
                            error_message=_token_check_error(token_check),
                            mode="login",
                            stage=state.get("stage", "idle"),
                            ready=False,
                        )
                return gopay_cycle_pb2.AuthStartResponse(
                    success=True,
                    mode="login",
                    stage=state.get("stage", "idle"),
                    otp_sent=bool(result.get("otp_sent")),
                    verification_id=result.get("verification_id", ""),
                    verification_method=result.get("method", ""),
                    ready=ready,
                )
            if not result.get("not_registered"):
                state = load_state()
                return gopay_cycle_pb2.AuthStartResponse(
                    success=False,
                    error_message=result.get("error", "login start failed"),
                    mode="login",
                    stage=state.get("stage", "idle"),
                )

            resp = self.SignupStart(
                gopay_cycle_pb2.SignupStartRequest(
                    phone=normalized_phone,
                    name=_signup_name(),
                    email=_signup_email(),
                    country_code=country_code,
                ),
                context,
            )
            state = load_state()
            return gopay_cycle_pb2.AuthStartResponse(
                success=resp.success,
                error_message=resp.error_message,
                mode="signup",
                stage=state.get("stage", "idle"),
                otp_sent=resp.otp_sent,
                verification_id=resp.verification_id,
                verification_method=resp.verification_method,
            )
        except Exception as e:
            return gopay_cycle_pb2.AuthStartResponse(success=False, error_message=str(e))

    def AuthComplete(self, request, context):
        try:
            state = load_state()
            changed = expire_login_if_needed(state) or expire_signup_if_needed(state)
            if changed:
                save_state(state)
                state = load_state()
            stage = str(state.get("stage", "")).strip()

            if stage == "ready" and _has_cycle_seed(state):
                token_check = check_token_valid(state)
                state = load_state()
                if _token_check_valid(token_check):
                    return gopay_cycle_pb2.AuthCompleteResponse(
                        success=True,
                        mode="token",
                        stage="ready",
                        phone=state.get("phone", ""),
                        ready=True,
                    )
                return gopay_cycle_pb2.AuthCompleteResponse(
                    success=False,
                    error_message=_token_check_error(token_check),
                    mode="token",
                    stage=state.get("stage", "ready"),
                    phone=state.get("phone", ""),
                )
            if stage == "login_otp_pending":
                resp = self.LoginComplete(
                    gopay_cycle_pb2.LoginCompleteRequest(otp=request.otp),
                    context,
                )
                state = load_state()
                ready = resp.success and state.get("stage") == "ready"
                if ready:
                    token_check = check_token_valid(state)
                    state = load_state()
                    if not _token_check_valid(token_check):
                        return gopay_cycle_pb2.AuthCompleteResponse(
                            success=False,
                            error_message=_token_check_error(token_check),
                            mode="login",
                            stage=state.get("stage", "idle"),
                            phone=resp.phone,
                        )
                return gopay_cycle_pb2.AuthCompleteResponse(
                    success=resp.success,
                    error_message=resp.error_message,
                    mode="login",
                    stage=state.get("stage", "idle"),
                    phone=resp.phone,
                    ready=ready,
                )

            if stage == "signup_otp_pending":
                resp = self.SignupComplete(
                    gopay_cycle_pb2.SignupCompleteRequest(otp=request.otp),
                    context,
                )
                state = load_state()
                ready = resp.success and state.get("stage") == "ready"
                if ready:
                    token_check = check_token_valid(state)
                    state = load_state()
                    if not _token_check_valid(token_check):
                        return gopay_cycle_pb2.AuthCompleteResponse(
                            success=False,
                            error_message=_token_check_error(token_check),
                            mode="signup",
                            stage=state.get("stage", "idle"),
                            phone=resp.phone,
                            pin_setup_required=resp.pin_setup_required,
                        )
                return gopay_cycle_pb2.AuthCompleteResponse(
                    success=resp.success,
                    error_message=resp.error_message,
                    mode="signup",
                    stage=state.get("stage", "idle"),
                    phone=resp.phone,
                    pin_setup_required=resp.pin_setup_required,
                    ready=ready,
                )
            if stage == "signup_pin_otp_pending":
                resp = self.CreatePinComplete(
                    gopay_cycle_pb2.CreatePinCompleteRequest(otp=request.otp, pin=request.pin),
                    context,
                )
                state = load_state()
                ready = resp.success and state.get("stage") == "ready"
                if ready:
                    token_check = check_token_valid(state)
                    state = load_state()
                    if not _token_check_valid(token_check):
                        return gopay_cycle_pb2.AuthCompleteResponse(
                            success=False,
                            error_message=_token_check_error(token_check),
                            mode="signup",
                            stage=state.get("stage", "idle"),
                            phone=resp.phone,
                            pin_setup_complete=resp.pin_setup_complete,
                        )
                return gopay_cycle_pb2.AuthCompleteResponse(
                    success=resp.success,
                    error_message=resp.error_message,
                    mode="signup",
                    stage=state.get("stage", "idle"),
                    phone=resp.phone,
                    pin_setup_complete=resp.pin_setup_complete,
                    ready=ready,
                )
            return gopay_cycle_pb2.AuthCompleteResponse(
                success=False,
                error_message=f"not waiting for auth otp: {stage or 'idle'}",
                stage=stage or "idle",
            )
        except Exception as e:
            return gopay_cycle_pb2.AuthCompleteResponse(success=False, error_message=str(e))

    def CheckTokenValid(self, request, context):
        try:
            state = load_state()
            result = check_token_valid(state)
            state = load_state()
            return gopay_cycle_pb2.CheckTokenValidResponse(
                success=bool(result.get("success")),
                error_message=result.get("error", ""),
                stage=state.get("stage", "idle"),
                phone=state.get("phone", ""),
                token_valid=bool(result.get("token_valid")),
                refreshed=bool(result.get("refreshed")),
                balance_amount=int(result.get("balance_amount") or state.get("balance_amount") or 0),
                has_min_balance=bool(result.get("has_min_balance")),
                balance_currency=result.get("balance_currency") or state.get("balance_currency", ""),
            )
        except Exception as e:
            return gopay_cycle_pb2.CheckTokenValidResponse(success=False, error_message=str(e))

    def Unlink(self, request, context):
        """解绑后标记 token 已消费；不会自动触发下一轮。"""
        try:
            state = load_state()
            c = _client(state)
            r = c.get(f"{GOPAY_CUSTOMER}/v1/linkedapps")
            count = 0
            if r["status"] == 200:
                for svc in r["data"].get("linked_services", []):
                    url = svc.get("unlink_service_url", "")
                    if url:
                        c.patch(f"{GOPAY_CUSTOMER}{url}")
                        count += 1
            state["stage"] = "consumed"
            state.pop("last_error", None)
            save_state(state)
            return gopay_cycle_pb2.UnlinkResponse(success=True, unlinked_count=count)
        except Exception as e:
            return gopay_cycle_pb2.UnlinkResponse(success=False, error_message=str(e))

    def Status(self, request, context):
        state = load_state()
        changed = expire_login_if_needed(state) or expire_signup_if_needed(state)
        if changed:
            save_state(state)
            state = load_state()
        if state.get("stage") == "ready":
            ensure_access_token(state)
            state = load_state()
        device = state.get("device", {})
        fp_parts = [
            device.get("profile_id", ""),
            device.get("x-phonemake", ""),
            device.get("x-phonemodel", ""),
            device.get("x-uniqueid", ""),
        ] if device else []
        fp = "/".join(str(part) for part in fp_parts if part)
        error_message = state.get("last_error", "")
        if state.get("last_token_refresh_error") and not _has_cycle_seed(state):
            error_message = state.get("last_token_refresh_error")
        return gopay_cycle_pb2.StatusResponse(
            stage=state.get("stage", "idle"),
            phone=state.get("phone", ""),
            device_fingerprint=fp,
            deactivated_at=state.get("deactivated_at", 0),
            error_message=error_message,
            token_present=_has_cycle_seed(state),
            login_otp_sent_at_unix=int(state.get("_login_otp_sent_at") or 0),
            login_otp_expires_at_unix=int(state.get("_login_otp_expires_at") or 0),
            signup_otp_sent_at_unix=int(state.get("_signup_otp_sent_at") or 0),
            signup_otp_expires_at_unix=int(state.get("_signup_otp_expires_at") or 0),
            signup_pin_otp_sent_at_unix=int(state.get("_signup_pin_otp_sent_at") or 0),
            signup_pin_otp_expires_at_unix=int(state.get("_signup_pin_otp_expires_at") or 0),
            balance_amount=int(state.get("balance_amount") or 0),
            has_min_balance=bool(state.get("has_min_balance")),
            balance_currency=state.get("balance_currency", ""),
        )

    def GetReadyAccessToken(self, request, context):
        state = load_state()
        if state.get("stage") == "ready":
            token_check = check_token_valid(state)
            state = load_state()
            if not token_check.get("success") or not token_check.get("token_valid"):
                return gopay_cycle_pb2.GetReadyAccessTokenResponse(
                    success=False,
                    error_message=token_check.get("error", "token validation failed"),
                )
            if not token_check.get("has_min_balance"):
                return gopay_cycle_pb2.GetReadyAccessTokenResponse(
                    success=False,
                    error_message=_token_check_error(token_check),
                )
        token = str(state.get("token") or "").strip()
        if state.get("stage") != "ready" or not token:
            return gopay_cycle_pb2.GetReadyAccessTokenResponse(
                success=False,
                error_message=f"cycle token not ready: stage={state.get('stage', 'idle')}",
            )
        if not access_token_usable(state, 0):
            expires_at = access_token_expires_at(token)
            return gopay_cycle_pb2.GetReadyAccessTokenResponse(
                success=False,
                error_message=f"cycle token expired: expires_at={expires_at}",
            )
        return gopay_cycle_pb2.GetReadyAccessTokenResponse(
            success=True,
            access_token=token,
            phone=state.get("phone", ""),
        )

    def CheckDeactivation(self, request, context):
        state = load_state()
        deactivated_at = state.get("deactivated_at", 0)
        if not deactivated_at:
            return gopay_cycle_pb2.CheckDeactivationResponse(completed=False, remaining_seconds=-1)
        return gopay_cycle_pb2.CheckDeactivationResponse(completed=True, remaining_seconds=0)


for _method_name in (
    "ChangePhoneStart",
    "ChangePhoneRetry",
    "ChangePhoneComplete",
    "DeactivateStart",
    "DeactivateComplete",
    "LoginStart",
    "LoginComplete",
    "SignupStart",
    "SignupComplete",
    "CreatePinStart",
    "CreatePinComplete",
    "AuthStart",
    "AuthComplete",
    "CheckTokenValid",
    "Unlink",
    "Status",
    "GetReadyAccessToken",
    "CheckDeactivation",
):
    setattr(
        GopayCycleServicer,
        _method_name,
        _stateful_rpc(getattr(GopayCycleServicer, _method_name)),
    )


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    gopay_cycle_pb2_grpc.add_GopayCycleServiceServicer_to_server(GopayCycleServicer(), server)
    server.add_insecure_port(f"0.0.0.0:{PORT}")
    server.start()
    print(f"[gopay-cycle] gRPC listening on :{PORT}, explicit steps enabled")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
