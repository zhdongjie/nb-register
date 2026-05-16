import unittest
from types import SimpleNamespace

from bot import TelegramCheckPhoneBot, format_check_response, format_gopay_status_response, parse_allowed_chat_ids, parse_check_text


class FakeTelegramBot(TelegramCheckPhoneBot):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("gopay", FakeGopayClient())
        super().__init__("token", *args, **kwargs)
        self.calls = []

    def _telegram(self, method: str, payload: dict, timeout: int = 30, attempts: int = 3) -> dict:
        self.calls.append((method, payload))
        return {"ok": True, "result": True}


class FakeGopayClient:
    def __init__(self):
        self.calls = []
        self.auth_start_response = SimpleNamespace(success=True, error_message="", ready=True, otp_sent=False, stage="ready")
        self.auth_complete_response = SimpleNamespace(success=True, error_message="", ready=True, phone="6281234567890", stage="ready")

    def auth_start(self, state_key, *, phone, country_code, pin):
        self.calls.append(("auth_start", state_key, phone, country_code, pin))
        return self.auth_start_response

    def auth_complete(self, state_key, *, otp, pin):
        self.calls.append(("auth_complete", state_key, otp, pin))
        return self.auth_complete_response

    def status(self, state_key):
        self.calls.append(("status", state_key))
        return SimpleNamespace(
            success=True,
            error_message="",
            status=SimpleNamespace(
                stage="ready",
                phone="81234567890",
                token_present=True,
                balance_amount=2,
                balance_currency="IDR",
                has_min_balance=True,
                error_message="",
            ),
        )

    def clear_state(self, state_key):
        self.calls.append(("clear_state", state_key))
        return SimpleNamespace(success=True, error_message="")


class TelegramBotParsingTests(unittest.TestCase):
    def test_parse_plain_phone_uses_default_country_code(self):
        parsed = parse_check_text("6289600000000", "62")

        self.assertEqual(parsed.phone, "6289600000000")
        self.assertEqual(parsed.country_code, "+62")

    def test_parse_gopay_registered_command_with_phone_is_not_accepted(self):
        parsed = parse_check_text("/check-gopay-registered 6281234567890", "62")

        self.assertIsNone(parsed)

    def test_parse_check_command_with_explicit_country_code_is_not_accepted(self):
        parsed = parse_check_text("/check-gopay-registered +62 89600000000", "+1")

        self.assertIsNone(parsed)

    def test_parse_menu_command_with_underscore_is_not_accepted(self):
        parsed = parse_check_text("/check_gopay_registered 6281234567890", "62")

        self.assertIsNone(parsed)

    def test_parse_help_returns_none(self):
        self.assertIsNone(parse_check_text("/help", "+62"))

    def test_parse_allowed_chat_ids_accepts_commas_and_spaces(self):
        self.assertEqual(parse_allowed_chat_ids("1, 2\n3"), {"1", "2", "3"})

    def test_format_registered_response(self):
        text = format_check_response("6289600000000", "+62", {
            "success": True,
            "available": False,
            "status": "registered",
        })

        self.assertIn("+6289600000000", text)
        self.assertIn("已注册", text)

    def test_check_command_prompts_then_next_message_checks_phone(self):
        checked = []

        def fake_checker(phone, country_code):
            checked.append((phone, country_code))
            return {"success": True, "available": False, "status": "registered"}

        bot = FakeTelegramBot(default_country_code="62", checker=fake_checker)
        bot.handle_update({
            "message": {
                "message_id": 1,
                "chat": {"id": 100},
                "from": {"id": 200},
                "text": "/check-gopay-registered",
            },
        })
        bot.handle_update({
            "message": {
                "message_id": 2,
                "chat": {"id": 100},
                "from": {"id": 200},
                "text": "6281234567890",
            },
        })

        self.assertEqual(checked, [("6281234567890", "+62")])
        messages = [payload["text"] for method, payload in bot.calls if method == "sendMessage"]
        self.assertIn("请发送要检测的手机号", messages[0])
        self.assertIn("已注册", messages[-1])

    def test_check_command_with_phone_only_prompts_and_does_not_check(self):
        checked = []
        bot = FakeTelegramBot(
            default_country_code="62",
            checker=lambda phone, country_code: checked.append((phone, country_code)),
        )

        bot.handle_update({
            "message": {
                "message_id": 1,
                "chat": {"id": 100},
                "from": {"id": 200},
                "text": "/check-gopay-registered 6281234567890",
            },
        })

        self.assertEqual(checked, [])
        messages = [payload["text"] for method, payload in bot.calls if method == "sendMessage"]
        self.assertEqual(len(messages), 1)
        self.assertIn("请发送要检测的手机号", messages[0])

    def test_gopay_login_is_public(self):
        bot = FakeTelegramBot(default_country_code="62")

        bot.handle_update({
            "message": {
                "message_id": 1,
                "chat": {"id": 100},
                "from": {"id": 200},
                "text": "/login-gopay",
            },
        })

        messages = [payload["text"] for method, payload in bot.calls if method == "sendMessage"]
        self.assertEqual(len(messages), 1)
        self.assertIn("请发送要登录的 GoPay 手机号", messages[0])

    def test_gopay_login_uses_orchestrator_state_key_per_user(self):
        gopay = FakeGopayClient()
        bot = FakeTelegramBot(default_country_code="62", gopay=gopay)

        bot.handle_update({
            "message": {
                "message_id": 1,
                "chat": {"id": 100},
                "from": {"id": 200},
                "text": "/login-gopay",
            },
        })
        bot.handle_update({
            "message": {
                "message_id": 2,
                "chat": {"id": 100},
                "from": {"id": 200},
                "text": "6281234567890",
            },
        })
        bot.handle_update({
            "message": {
                "message_id": 3,
                "chat": {"id": 100},
                "from": {"id": 200},
                "text": "123456",
            },
        })

        self.assertEqual(gopay.calls, [("auth_start", "tg:200", "6281234567890", "+62", "123456")])
        messages = [payload["text"] for method, payload in bot.calls if method == "sendMessage"]
        self.assertIn("请发送要登录的 GoPay 手机号", messages[0])
        self.assertIn("请发送这个 GoPay 账号的 PIN", messages[1])
        self.assertIn("GoPay token 已就绪", messages[-1])

    def test_gopay_login_unregistered_stops_without_signup_or_pin(self):
        gopay = FakeGopayClient()
        gopay.auth_start_response = SimpleNamespace(
            success=False,
            error_message="账户未注册",
            ready=False,
            otp_sent=False,
            stage="idle",
        )
        bot = FakeTelegramBot(default_country_code="62", gopay=gopay)

        bot.handle_update({
            "message": {
                "message_id": 1,
                "chat": {"id": 100},
                "from": {"id": 200},
                "text": "/login-gopay",
            },
        })
        bot.handle_update({
            "message": {
                "message_id": 2,
                "chat": {"id": 100},
                "from": {"id": 200},
                "text": "6281234567890",
            },
        })
        bot.handle_update({
            "message": {
                "message_id": 3,
                "chat": {"id": 100},
                "from": {"id": 200},
                "text": "123456",
            },
        })

        self.assertEqual(gopay.calls, [("auth_start", "tg:200", "6281234567890", "+62", "123456")])
        messages = [payload["text"] for method, payload in bot.calls if method == "sendMessage"]
        self.assertIn("登录失败：账户未注册", messages[-1])
        self.assertNotIn("创建 PIN", "\n".join(messages))

    def test_format_gopay_status(self):
        text = format_gopay_status_response(SimpleNamespace(
            success=True,
            error_message="",
            status=SimpleNamespace(
                stage="ready",
                phone="81234567890",
                token_present=True,
                balance_amount=1,
                balance_currency="IDR",
                has_min_balance=True,
                error_message="",
            ),
        ))

        self.assertIn("阶段：ready", text)
        self.assertIn("Token：有", text)


if __name__ == "__main__":
    unittest.main()
