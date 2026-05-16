import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

import orchestrator_client
from checkphone import _country_code, _normalize_phone, check_phone_by_login_methods


PHONE_RE = re.compile(r"\+?\d[\d\s().-]{4,}\d")
CHECK_COMMAND = "/check-gopay-registered"
LOGIN_GOPAY_COMMAND = "/login-gopay"
CHANGE_GOPAY_PHONE_COMMAND = "/change-gopay-phone"
SIGNUP_GOPAY_COMMAND = "/signup-gopay"
CREATE_GOPAY_PIN_COMMAND = "/create-gopay-pin"
GOPAY_STATUS_COMMAND = "/gopay-status"
CLEAR_GOPAY_STATE_COMMAND = "/clear-gopay-state"
SET_GOPAY_WA_PHONE_COMMAND = "/set-gopay-wa-phone"
GOPAY_COMMANDS = {
    LOGIN_GOPAY_COMMAND,
    CHANGE_GOPAY_PHONE_COMMAND,
    SIGNUP_GOPAY_COMMAND,
    CREATE_GOPAY_PIN_COMMAND,
    GOPAY_STATUS_COMMAND,
    CLEAR_GOPAY_STATE_COMMAND,
    SET_GOPAY_WA_PHONE_COMMAND,
}
COMMANDS = {"/start", "/help", CHECK_COMMAND} | GOPAY_COMMANDS
CHECK_COMMANDS = {CHECK_COMMAND}
PENDING_TTL_SECONDS = int(os.environ.get("TELEGRAM_PENDING_SECONDS", "300"))


@dataclass
class CheckRequest:
    phone: str
    country_code: str


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "")
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _proxy_map(proxy: str) -> Optional[dict]:
    value = str(proxy or "").strip()
    if not value:
        return None
    if value.startswith("socks5://"):
        value = "socks5h://" + value[len("socks5://"):]
    return {"http": value, "https": value}


def parse_allowed_chat_ids(value: str) -> set[str]:
    return {part.strip() for part in re.split(r"[,\s]+", value or "") if part.strip()}


def _strip_bot_mention(command: str) -> str:
    return command.split("@", 1)[0].lower()


def _redact_token(text: object) -> str:
    value = str(text)
    return re.sub(r"/bot[^/\s]+/", "/bot<redacted>/", value)


def parse_check_text(text: str, default_country_code: str) -> Optional[CheckRequest]:
    raw = str(text or "").strip()
    if not raw:
        return None

    parts = raw.split(maxsplit=1)
    if parts:
        command = _strip_bot_mention(parts[0])
        if command.startswith("/"):
            return None

    tokens = raw.split()
    country_code = _country_code(default_country_code)
    if len(tokens) >= 2 and re.fullmatch(r"\+?\d{1,4}", tokens[0]):
        country_code = _country_code(tokens[0])
        raw = " ".join(tokens[1:])

    match = PHONE_RE.search(raw)
    if not match:
        return None
    phone = re.sub(r"\D", "", match.group())
    if not phone:
        return None
    return CheckRequest(phone=phone, country_code=country_code)


def usage_text(default_country_code: str) -> str:
    cc = _country_code(default_country_code)
    return (
        "检测 GoPay 手机号是否已注册：\n"
        f"1. 发送 {CHECK_COMMAND}\n"
        "2. 按提示发送手机号\n\n"
        "支持格式：628xxxxxxxxxx、8xxxxxxxxxx、+62 8xxxxxxxxxx\n"
        f"当前默认区号：{cc}"
    )


def gopay_usage_text(default_country_code: str) -> str:
    return (
        usage_text(default_country_code)
        + "\n\nGoPay 用户流程：\n"
        f"{LOGIN_GOPAY_COMMAND} - 登录；未注册返回账户未注册\n"
        f"{CHANGE_GOPAY_PHONE_COMMAND} - 换绑手机号\n"
        f"{SIGNUP_GOPAY_COMMAND} - 注册账号\n"
        f"{CREATE_GOPAY_PIN_COMMAND} - 创建 PIN\n"
        f"{SET_GOPAY_WA_PHONE_COMMAND} - 保存 WA 支付手机号\n"
        f"{GOPAY_STATUS_COMMAND} - 查看当前 GoPay state\n"
        f"{CLEAR_GOPAY_STATE_COMMAND} - 清空自己的 GoPay state"
    )


def phone_prompt_text(default_country_code: str) -> str:
    cc = _country_code(default_country_code)
    return (
        "请发送要检测的手机号。\n"
        "支持格式：628xxxxxxxxxx、8xxxxxxxxxx、+62 8xxxxxxxxxx\n"
        f"当前默认区号：{cc}"
    )


def gopay_login_phone_prompt(default_country_code: str) -> str:
    cc = _country_code(default_country_code)
    return (
        "请发送要登录的 GoPay 手机号。\n"
        "支持格式：628xxxxxxxxxx、8xxxxxxxxxx、+62 8xxxxxxxxxx\n"
        f"当前默认区号：{cc}"
    )


def gopay_pin_prompt() -> str:
    return "请发送这个 GoPay 账号的 PIN。"


def gopay_otp_prompt(method: str = "") -> str:
    suffix = f"（{method}）" if method else ""
    return f"已通过 PIN，等待登录 OTP{suffix}。请发送 OTP。"


def gopay_change_phone_prompt(default_country_code: str) -> str:
    cc = _country_code(default_country_code)
    return (
        "请发送要换绑的新手机号。\n"
        "支持格式：628xxxxxxxxxx、8xxxxxxxxxx、+62 8xxxxxxxxxx\n"
        f"当前默认区号：{cc}"
    )


def gopay_wa_phone_prompt(default_country_code: str) -> str:
    cc = _country_code(default_country_code)
    return (
        "请发送 WA 支付注册手机号。\n"
        "支持格式：628xxxxxxxxxx、8xxxxxxxxxx、+62 8xxxxxxxxxx\n"
        f"当前默认区号：{cc}"
    )


def gopay_signup_phone_prompt(default_country_code: str) -> str:
    cc = _country_code(default_country_code)
    return (
        "请发送要注册的 GoPay 手机号。\n"
        "支持格式：628xxxxxxxxxx、8xxxxxxxxxx、+62 8xxxxxxxxxx\n"
        f"当前默认区号：{cc}"
    )


def format_check_response(phone: str, country_code: str, result: dict) -> str:
    cc = _country_code(country_code)
    normalized = _normalize_phone(phone, cc)
    display_phone = f"{cc}{normalized}"
    status = str(result.get("status") or "error")
    if result.get("success") and result.get("available"):
        return f"{display_phone}\n状态：可用（未注册）"
    if result.get("success") and status == "registered":
        return f"{display_phone}\n状态：已注册"
    if status == "rate_limited":
        return f"{display_phone}\n状态：限流\n错误：{result.get('error') or 'RATE_LIMITED'}"
    return f"{display_phone}\n状态：检测失败\n错误：{result.get('error') or result.get('error_message') or 'unknown error'}"


def _response_error(resp) -> str:
    return str(getattr(resp, "error_message", "") or "unknown error")


def _response_success(resp) -> bool:
    return bool(getattr(resp, "success", False))


def format_gopay_status_response(resp) -> str:
    if not _response_success(resp):
        return f"GoPay state 查询失败：{_response_error(resp)}"
    status = getattr(resp, "status", None)
    if status is None:
        return "GoPay state 查询失败：empty status"
    lines = [
        f"阶段：{getattr(status, 'stage', '') or 'idle'}",
        f"手机号：{getattr(status, 'phone', '') or '-'}",
        f"Token：{'有' if getattr(status, 'token_present', False) else '无'}",
    ]
    amount = int(getattr(status, "balance_amount", 0) or 0)
    currency = getattr(status, "balance_currency", "") or "IDR"
    if amount or getattr(status, "has_min_balance", False):
        lines.append(f"余额：{amount} {currency}")
    error = str(getattr(status, "error_message", "") or "").strip()
    if error:
        lines.append(f"错误：{error}")
    return "\n".join(lines)


def format_gopay_start_response(resp, action: str) -> str:
    if not _response_success(resp):
        return f"{action}失败：{_response_error(resp)}"
    if getattr(resp, "ready", False):
        return f"{action}完成：GoPay token 已就绪。"
    if getattr(resp, "otp_sent", False):
        method = str(getattr(resp, "verification_method", "") or "").strip()
        suffix = f"（{method}）" if method else ""
        return f"{action}已发送 OTP{suffix}。请发送 OTP。"
    return f"{action}已提交，当前阶段：{getattr(resp, 'stage', '') or 'unknown'}"


def format_gopay_complete_response(resp, action: str) -> str:
    if not _response_success(resp):
        return f"{action}失败：{_response_error(resp)}"
    phone = str(getattr(resp, "phone", "") or "").strip()
    prefix = f"手机号：{phone}\n" if phone else ""
    if getattr(resp, "ready", False) or getattr(resp, "pin_setup_complete", False):
        return f"{prefix}{action}完成。"
    if getattr(resp, "pin_setup_required", False):
        return f"{prefix}注册完成，需要继续创建 PIN。"
    stage = str(getattr(resp, "stage", "") or "").strip()
    return f"{prefix}{action}已提交，当前阶段：{stage or 'unknown'}"


class TelegramCheckPhoneBot:
    def __init__(
        self,
        token: str,
        *,
        api_base: str = "https://api.telegram.org",
        telegram_proxy: str = "",
        default_country_code: str = "+62",
        allowed_chat_ids: Optional[set[str]] = None,
        poll_timeout: int = 30,
        poll_limit: int = 20,
        checker: Callable[[str, str], dict] = check_phone_by_login_methods,
        gopay: Optional[orchestrator_client.OrchestratorGopayClient] = None,
    ):
        self.token = token.strip()
        self.api_base = api_base.rstrip("/")
        self.telegram_proxy = telegram_proxy
        self.default_country_code = _country_code(default_country_code)
        self.allowed_chat_ids = allowed_chat_ids or set()
        self.poll_timeout = max(1, poll_timeout)
        self.poll_limit = max(1, min(100, poll_limit))
        self.checker = checker
        self.gopay = gopay or orchestrator_client.OrchestratorGopayClient(
            timeout=int(os.environ.get("ORCHESTRATOR_GOPAY_TIMEOUT_SECONDS", "120"))
        )
        self._stopping = False
        self._pending_checks: dict[tuple[int, str], float] = {}
        self._pending_gopay_flows: dict[tuple[int, str], dict] = {}

    def stop(self, *_args) -> None:
        self._stopping = True

    def _api_url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    def _telegram(self, method: str, payload: dict, timeout: int = 30, attempts: int = 3) -> dict:
        last_error = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                response = requests.post(
                    self._api_url(method),
                    json=payload,
                    proxies=_proxy_map(self.telegram_proxy),
                    timeout=timeout,
                )
                response.raise_for_status()
                data = response.json()
                if not data.get("ok"):
                    raise RuntimeError(f"telegram {method} failed: {data.get('description') or data}")
                return data
            except Exception as e:
                last_error = e
                if attempt >= max(1, attempts):
                    break
                time.sleep(min(2 * attempt, 5))
        raise RuntimeError(f"telegram {method} failed after retries: {_redact_token(last_error)}")

    def delete_webhook(self, drop_pending_updates: bool) -> None:
        self._telegram("deleteWebhook", {"drop_pending_updates": drop_pending_updates}, timeout=15)

    def configure_menu(self) -> None:
        commands = [
            {"command": "help", "description": "查看使用说明"},
        ]
        self._telegram("setMyCommands", {"commands": commands}, timeout=15)
        description = f"发送 {CHECK_COMMAND}，再按提示发送手机号，检测 GoPay 是否已注册。"
        self._telegram("setMyDescription", {"description": description}, timeout=15)
        self._telegram("setMyShortDescription", {"short_description": "GoPay 手机号注册检测"}, timeout=15)

    def get_updates(self, offset: Optional[int]) -> list[dict]:
        payload = {
            "timeout": self.poll_timeout,
            "limit": self.poll_limit,
            "allowed_updates": ["message", "edited_message"],
        }
        if offset is not None:
            payload["offset"] = offset
        data = self._telegram("getUpdates", payload, timeout=self.poll_timeout + 10, attempts=2)
        return data.get("result") or []

    def send_message(self, chat_id: int, text: str, reply_to_message_id: Optional[int] = None) -> None:
        payload = {"chat_id": chat_id, "text": text}
        if reply_to_message_id:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        self._telegram("sendMessage", payload, timeout=15)

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            self._telegram("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=10, attempts=2)
        except Exception as e:
            print(f"[checkphone-tgbot] sendChatAction ignored: {_redact_token(e)}", flush=True)

    def _pending_key(self, message: dict, chat_id: int) -> tuple[int, str]:
        user = message.get("from") or {}
        user_id = str(user.get("id") or chat_id)
        return int(chat_id), user_id

    def _set_pending_check(self, message: dict, chat_id: int) -> None:
        self._pending_checks[self._pending_key(message, chat_id)] = time.time() + PENDING_TTL_SECONDS

    def _pop_pending_check(self, message: dict, chat_id: int) -> bool:
        key = self._pending_key(message, chat_id)
        expires_at = self._pending_checks.get(key)
        if not expires_at:
            return False
        if expires_at < time.time():
            self._pending_checks.pop(key, None)
            return False
        self._pending_checks.pop(key, None)
        return True

    def _set_pending_gopay_flow(self, message: dict, chat_id: int, session: dict) -> None:
        session = dict(session)
        session["expires_at"] = time.time() + PENDING_TTL_SECONDS
        self._pending_gopay_flows[self._pending_key(message, chat_id)] = session

    def _get_pending_gopay_flow(self, message: dict, chat_id: int) -> Optional[dict]:
        key = self._pending_key(message, chat_id)
        session = self._pending_gopay_flows.get(key)
        if not session:
            return None
        if float(session.get("expires_at") or 0) < time.time():
            self._pending_gopay_flows.pop(key, None)
            return None
        return session

    def _clear_pending_gopay_flow(self, message: dict, chat_id: int) -> None:
        self._pending_gopay_flows.pop(self._pending_key(message, chat_id), None)

    def _gopay_state_key(self, message: dict, chat_id: int) -> str:
        _chat_id, user_id = self._pending_key(message, chat_id)
        return f"tg:{user_id}"

    def _handle_gopay_command(self, first: str, text: str, message: dict, chat_id: int) -> bool:
        if first not in GOPAY_COMMANDS:
            return False

        if first == LOGIN_GOPAY_COMMAND:
            self._set_pending_gopay_flow(message, chat_id, {"step": "auth_phone"})
            self.send_message(chat_id, gopay_login_phone_prompt(self.default_country_code), message.get("message_id"))
            return True
        if first == CHANGE_GOPAY_PHONE_COMMAND:
            self._set_pending_gopay_flow(message, chat_id, {"step": "change_phone"})
            self.send_message(chat_id, gopay_change_phone_prompt(self.default_country_code), message.get("message_id"))
            return True
        if first == SIGNUP_GOPAY_COMMAND:
            self._set_pending_gopay_flow(message, chat_id, {"step": "signup_phone"})
            self.send_message(chat_id, gopay_signup_phone_prompt(self.default_country_code), message.get("message_id"))
            return True
        if first == CREATE_GOPAY_PIN_COMMAND:
            self._set_pending_gopay_flow(message, chat_id, {"step": "create_pin_pin"})
            self.send_message(chat_id, gopay_pin_prompt(), message.get("message_id"))
            return True
        if first == SET_GOPAY_WA_PHONE_COMMAND:
            self._set_pending_gopay_flow(message, chat_id, {"step": "set_wa_phone"})
            self.send_message(chat_id, gopay_wa_phone_prompt(self.default_country_code), message.get("message_id"))
            return True
        if first == GOPAY_STATUS_COMMAND:
            self.send_chat_action(chat_id)
            try:
                resp = self.gopay.status(self._gopay_state_key(message, chat_id))
                self.send_message(chat_id, format_gopay_status_response(resp), message.get("message_id"))
            except Exception as e:
                self.send_message(chat_id, f"GoPay state 查询失败：{_redact_token(e)}", message.get("message_id"))
            return True
        if first == CLEAR_GOPAY_STATE_COMMAND:
            self._clear_pending_gopay_flow(message, chat_id)
            self.send_chat_action(chat_id)
            try:
                resp = self.gopay.clear_state(self._gopay_state_key(message, chat_id))
                if _response_success(resp):
                    self.send_message(chat_id, "已清空你的 GoPay state。", message.get("message_id"))
                else:
                    self.send_message(chat_id, f"清空 GoPay state 失败：{_response_error(resp)}", message.get("message_id"))
            except Exception as e:
                self.send_message(chat_id, f"清空 GoPay state 失败：{_redact_token(e)}", message.get("message_id"))
            return True
        return False

    def _call_orchestrator(self, chat_id: int, message_id: Optional[int], fn: Callable[[], object]):
        try:
            self.send_chat_action(chat_id)
            return fn()
        except Exception as e:
            self.send_message(chat_id, f"GoPay 编排调用失败：{_redact_token(e)}", message_id)
            return None

    def _handle_pending_gopay_flow(self, text: str, message: dict, chat_id: int) -> bool:
        session = self._get_pending_gopay_flow(message, chat_id)
        if not session:
            return False

        step = session.get("step")
        state_key = self._gopay_state_key(message, chat_id)
        message_id = message.get("message_id")

        if step in {"auth_phone", "signup_phone", "change_phone", "set_wa_phone"}:
            request = parse_check_text(text, self.default_country_code)
            if request is None:
                self._set_pending_gopay_flow(message, chat_id, session)
                if step == "change_phone":
                    self.send_message(chat_id, gopay_change_phone_prompt(self.default_country_code), message_id)
                elif step == "signup_phone":
                    self.send_message(chat_id, gopay_signup_phone_prompt(self.default_country_code), message_id)
                elif step == "set_wa_phone":
                    self.send_message(chat_id, gopay_wa_phone_prompt(self.default_country_code), message_id)
                else:
                    self.send_message(chat_id, gopay_login_phone_prompt(self.default_country_code), message_id)
                return True
            if step == "set_wa_phone":
                resp = self._call_orchestrator(
                    chat_id,
                    message_id,
                    lambda: self.gopay.set_wa_phone(state_key, wa_phone=request.phone),
                )
                self._clear_pending_gopay_flow(message, chat_id)
                if resp is not None:
                    if _response_success(resp):
                        self.send_message(chat_id, f"已保存 WA 支付手机号：{getattr(resp, 'wa_phone', request.phone)}", message_id)
                    else:
                        self.send_message(chat_id, f"保存 WA 支付手机号失败：{_response_error(resp)}", message_id)
                return True
            next_step = {
                "auth_phone": "auth_pin",
                "signup_phone": "signup_pin",
                "change_phone": "change_pin",
            }[step]
            self._set_pending_gopay_flow(
                message,
                chat_id,
                {"step": next_step, "phone": request.phone, "country_code": request.country_code},
            )
            self.send_message(chat_id, gopay_pin_prompt(), message_id)
            return True

        if step in {"auth_pin", "signup_pin", "change_pin", "create_pin_pin"}:
            pin = re.sub(r"\D", "", text)
            if not pin:
                self._set_pending_gopay_flow(message, chat_id, session)
                self.send_message(chat_id, gopay_pin_prompt(), message_id)
                return True

            if step == "auth_pin":
                resp = self._call_orchestrator(
                    chat_id,
                    message_id,
                    lambda: self.gopay.auth_start(
                        state_key,
                        phone=str(session.get("phone") or ""),
                        country_code=str(session.get("country_code") or self.default_country_code),
                        pin=pin,
                    ),
                )
                if resp is None:
                    self._clear_pending_gopay_flow(message, chat_id)
                    return True
                if _response_success(resp) and getattr(resp, "otp_sent", False):
                    self._set_pending_gopay_flow(
                        message,
                        chat_id,
                        {
                            "step": "auth_otp",
                            "pin": pin,
                            "phone": session.get("phone", ""),
                            "country_code": session.get("country_code", self.default_country_code),
                        },
                    )
                else:
                    self._clear_pending_gopay_flow(message, chat_id)
                self.send_message(chat_id, format_gopay_start_response(resp, "登录"), message_id)
                return True

            if step == "signup_pin":
                resp = self._call_orchestrator(
                    chat_id,
                    message_id,
                    lambda: self.gopay.signup_start(
                        state_key,
                        phone=str(session.get("phone") or ""),
                        name="",
                        email="",
                        country_code=str(session.get("country_code") or self.default_country_code),
                    ),
                )
                if resp is None:
                    self._clear_pending_gopay_flow(message, chat_id)
                    return True
                if _response_success(resp) and getattr(resp, "otp_sent", False):
                    self._set_pending_gopay_flow(message, chat_id, {"step": "signup_otp", "pin": pin})
                else:
                    self._clear_pending_gopay_flow(message, chat_id)
                self.send_message(chat_id, format_gopay_start_response(resp, "注册"), message_id)
                return True

            if step == "change_pin":
                resp = self._call_orchestrator(
                    chat_id,
                    message_id,
                    lambda: self.gopay.change_phone_start(
                        state_key,
                        new_phone=str(session.get("phone") or ""),
                        pin=pin,
                    ),
                )
                if resp is None:
                    self._clear_pending_gopay_flow(message, chat_id)
                    return True
                if _response_success(resp) and getattr(resp, "otp_sent", False):
                    self._set_pending_gopay_flow(message, chat_id, {"step": "change_otp"})
                else:
                    self._clear_pending_gopay_flow(message, chat_id)
                self.send_message(chat_id, format_gopay_start_response(resp, "换绑"), message_id)
                return True

            resp = self._call_orchestrator(
                chat_id,
                message_id,
                lambda: self.gopay.create_pin_start(state_key, pin=pin),
            )
            if resp is None:
                self._clear_pending_gopay_flow(message, chat_id)
                return True
            if _response_success(resp) and getattr(resp, "otp_sent", False):
                self._set_pending_gopay_flow(message, chat_id, {"step": "create_pin_otp", "pin": pin})
            else:
                self._clear_pending_gopay_flow(message, chat_id)
            self.send_message(chat_id, format_gopay_start_response(resp, "创建 PIN"), message_id)
            return True

        if step in {"auth_otp", "signup_otp", "signup_pin_otp", "change_otp", "create_pin_otp"}:
            otp = re.sub(r"\D", "", text)
            if not otp:
                self._set_pending_gopay_flow(message, chat_id, session)
                self.send_message(chat_id, gopay_otp_prompt(), message_id)
                return True

            pin = str(session.get("pin") or "")
            if step == "auth_otp":
                resp = self._call_orchestrator(
                    chat_id,
                    message_id,
                    lambda: self.gopay.auth_complete(state_key, otp=otp, pin=pin),
                )
                if resp is None:
                    self._clear_pending_gopay_flow(message, chat_id)
                    return True
                self._clear_pending_gopay_flow(message, chat_id)
                self.send_message(chat_id, format_gopay_complete_response(resp, "登录"), message_id)
                return True

            if step == "signup_otp":
                resp = self._call_orchestrator(
                    chat_id,
                    message_id,
                    lambda: self.gopay.signup_complete(state_key, otp=otp),
                )
                if resp is None:
                    self._clear_pending_gopay_flow(message, chat_id)
                    return True
                if _response_success(resp) and getattr(resp, "pin_setup_required", False):
                    start_resp = self._call_orchestrator(
                        chat_id,
                        message_id,
                        lambda: self.gopay.create_pin_start(state_key, pin=pin),
                    )
                    if start_resp is not None and _response_success(start_resp) and getattr(start_resp, "otp_sent", False):
                        self._set_pending_gopay_flow(message, chat_id, {"step": "signup_pin_otp", "pin": pin})
                        self.send_message(chat_id, format_gopay_start_response(start_resp, "创建 PIN"), message_id)
                        return True
                    self._clear_pending_gopay_flow(message, chat_id)
                    self.send_message(chat_id, format_gopay_start_response(start_resp, "创建 PIN") if start_resp else "创建 PIN 失败。", message_id)
                    return True
                self._clear_pending_gopay_flow(message, chat_id)
                self.send_message(chat_id, format_gopay_complete_response(resp, "注册"), message_id)
                return True

            if step in {"signup_pin_otp", "create_pin_otp"}:
                resp = self._call_orchestrator(
                    chat_id,
                    message_id,
                    lambda: self.gopay.create_pin_complete(state_key, otp=otp, pin=pin),
                )
                self._clear_pending_gopay_flow(message, chat_id)
                if resp is not None:
                    self.send_message(chat_id, format_gopay_complete_response(resp, "创建 PIN"), message_id)
                return True

            resp = self._call_orchestrator(
                chat_id,
                message_id,
                lambda: self.gopay.change_phone_complete(state_key, otp=otp),
            )
            self._clear_pending_gopay_flow(message, chat_id)
            if resp is not None:
                if _response_success(resp):
                    self.send_message(chat_id, "换绑完成。", message_id)
                else:
                    self.send_message(chat_id, f"换绑失败：{_response_error(resp)}", message_id)
            return True

        self._clear_pending_gopay_flow(message, chat_id)
        return False

    def handle_update(self, update: dict) -> None:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return

        if self.allowed_chat_ids and str(chat_id) not in self.allowed_chat_ids:
            print(f"[checkphone-tgbot] ignoring unauthorized chat_id={chat_id}", flush=True)
            return

        text = str(message.get("text") or "").strip()
        if not text:
            return

        first = _strip_bot_mention(text.split(maxsplit=1)[0])
        if first in {"/start", "/help"}:
            print(f"[checkphone-tgbot] help chat_id={chat_id}", flush=True)
            help_text = gopay_usage_text(self.default_country_code)
            self.send_message(chat_id, help_text, message.get("message_id"))
            return

        if self._handle_gopay_command(first, text, message, chat_id):
            return

        if self._handle_pending_gopay_flow(text, message, chat_id):
            return

        if first in CHECK_COMMANDS:
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                self.send_message(chat_id, phone_prompt_text(self.default_country_code), message.get("message_id"))
                return
            self._set_pending_check(message, chat_id)
            self.send_message(chat_id, phone_prompt_text(self.default_country_code), message.get("message_id"))
            return
        elif self._pop_pending_check(message, chat_id):
            request = parse_check_text(text, self.default_country_code)
            if request is None:
                self._set_pending_check(message, chat_id)
                self.send_message(chat_id, phone_prompt_text(self.default_country_code), message.get("message_id"))
                return
        else:
            request = parse_check_text(text, self.default_country_code)

        if request is None:
            if first.startswith("/"):
                self.send_message(chat_id, usage_text(self.default_country_code), message.get("message_id"))
            return

        self.send_chat_action(chat_id)
        print(f"[checkphone-tgbot] check chat_id={chat_id} country_code={request.country_code}", flush=True)
        result = self.checker(request.phone, request.country_code)
        self.send_message(
            chat_id,
            format_check_response(request.phone, request.country_code, result),
            message.get("message_id"),
        )

    def run(self, drop_pending_updates: bool = True) -> None:
        self.delete_webhook(drop_pending_updates)
        try:
            self.configure_menu()
        except Exception as e:
            print(f"[checkphone-tgbot] configure menu ignored: {_redact_token(e)}", flush=True)
        offset = None
        print("[checkphone-tgbot] long polling started", flush=True)
        while not self._stopping:
            try:
                updates = self.get_updates(offset)
                for update in updates:
                    update_id = update.get("update_id")
                    try:
                        self.handle_update(update)
                        if isinstance(update_id, int):
                            offset = max(offset or 0, update_id + 1)
                    except Exception as e:
                        print(f"[checkphone-tgbot] update handling failed: {_redact_token(e)}", flush=True)
            except Exception as e:
                print(f"[checkphone-tgbot] polling failed: {_redact_token(e)}", flush=True)
                time.sleep(5)


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is required", file=sys.stderr)
        return 2

    bot = TelegramCheckPhoneBot(
        token,
        api_base=os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org"),
        telegram_proxy=os.environ.get("TELEGRAM_PROXY", ""),
        default_country_code=os.environ.get("GOPAY_COUNTRY_CODE", "62"),
        allowed_chat_ids=parse_allowed_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
        poll_timeout=int(os.environ.get("TELEGRAM_POLL_TIMEOUT_SECONDS", "30")),
        poll_limit=int(os.environ.get("TELEGRAM_POLL_LIMIT", "20")),
    )
    signal.signal(signal.SIGTERM, bot.stop)
    signal.signal(signal.SIGINT, bot.stop)
    bot.run(drop_pending_updates=_env_bool("TELEGRAM_DROP_PENDING_UPDATES", True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
