import base64
import json
import time
import unittest
from unittest.mock import patch

import gopay_client
import gopay_cycle
import replay

try:
    import cycle_server
except ImportError as exc:
    cycle_server = None
    CYCLE_SERVER_IMPORT_ERROR = exc
else:
    CYCLE_SERVER_IMPORT_ERROR = None


TEST_LOCAL_PHONE = "80000000000"
TEST_FULL_PHONE = "6280000000000"
TEST_E164_PHONE = f"+{TEST_FULL_PHONE}"
TEST_PIN = "000000"
TEST_CHANGE_LOCAL_PHONE = "89600000000"
TEST_CHANGE_FULL_PHONE = f"62{TEST_CHANGE_LOCAL_PHONE}"


def jwt_with_exp(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"{header}.{payload}."


class TokenRefreshTests(unittest.TestCase):
    def test_access_token_usable_checks_exp(self):
        state = {"token": jwt_with_exp(int(time.time()) + 120)}

        self.assertTrue(gopay_cycle.access_token_usable(state, 30))
        self.assertFalse(gopay_cycle.access_token_usable(state, 180))

    def test_ensure_access_token_refreshes_near_expiry(self):
        state = {
            "token": jwt_with_exp(int(time.time()) + 60),
            "refresh_token": "refresh-token",
        }

        def fake_refresh(target):
            target["token"] = jwt_with_exp(int(time.time()) + 3600)
            return {"success": True, "refreshed": True}

        with patch.object(gopay_cycle, "save_state", lambda target: None), \
                patch.object(gopay_cycle, "refresh_access_token", fake_refresh):
            result = gopay_cycle.ensure_access_token(state, min_ttl_seconds=300)

        self.assertTrue(result["success"])
        self.assertTrue(result["refreshed"])
        self.assertTrue(gopay_cycle.access_token_usable(state, 300))

    def test_ensure_access_token_keeps_valid_token(self):
        state = {
            "token": jwt_with_exp(int(time.time()) + 3600),
            "refresh_token": "refresh-token",
        }

        with patch.object(gopay_cycle, "save_state", lambda target: None), \
                patch.object(gopay_cycle, "refresh_access_token") as refresh:
            result = gopay_cycle.ensure_access_token(state, min_ttl_seconds=300)

        self.assertTrue(result["success"])
        self.assertFalse(result["refreshed"])
        refresh.assert_not_called()

    def test_store_token_response_replaces_stale_refresh_token(self):
        state = {
            "token": "old-token",
            "refresh_token": "old-refresh-token",
        }

        gopay_cycle._store_token_response(state, {
            "access_token": jwt_with_exp(int(time.time()) + 3600),
        })

        self.assertNotEqual(state["token"], "old-token")
        self.assertNotIn("refresh_token", state)

    def test_check_token_valid_checks_profile_before_refresh(self):
        state = {"token": "access-token", "device": {}}
        calls = []

        class FakeClient:
            def __init__(self, token, proxy=None, device=None):
                self.token = token

            def get(self, url, **kwargs):
                calls.append(("get", self.token, url))
                if url.endswith("/v1/payment-options/balances"):
                    return {"status": 200, "data": [
                        {"type": "GOPAY_WALLET", "balance": {"value": 1, "currency": "IDR"}},
                        {"type": "DEBIT", "balance": {"value": 1, "currency": "IDR"}},
                    ], "raw": {"success": True}}
                return {"status": 200, "data": {"phone": TEST_E164_PHONE}}

        with patch.object(gopay_cycle, "GopayClient", FakeClient), \
                patch.object(gopay_cycle, "save_state", lambda target: None):
            result = gopay_cycle.check_token_valid(state)

        self.assertTrue(result["success"])
        self.assertTrue(result["token_valid"])
        self.assertFalse(result["refreshed"])
        self.assertTrue(result["has_min_balance"])
        self.assertEqual(result["balance_amount"], 1)
        self.assertEqual(state["stage"], "ready")
        self.assertEqual(state["phone"], TEST_LOCAL_PHONE)
        self.assertEqual(calls, [
            ("get", "access-token", "https://customer.gopayapi.com/v1/users/profile"),
            ("get", "access-token", "https://customer.gopayapi.com/v1/payment-options/balances"),
        ])

    def test_check_token_valid_refreshes_then_rechecks_profile(self):
        state = {"token": "expired-token", "refresh_token": "refresh-token", "device": {}}
        calls = []

        class FakeClient:
            def __init__(self, token, proxy=None, device=None):
                self.token = token

            def get(self, url, **kwargs):
                calls.append(("get", self.token, url))
                if url.endswith("/v1/payment-options/balances"):
                    return {"status": 200, "data": [
                        {"type": "GOPAY_WALLET", "balance": {"value": 1, "currency": "IDR"}},
                    ], "raw": {"success": True}}
                if self.token == "new-token":
                    return {"status": 200, "data": {"phone": TEST_E164_PHONE}}
                return {"status": 401, "data": {"errors": [{"message": "unauthorized"}]}}

            def post(self, url, body=None, **kwargs):
                calls.append(("post", self.token, url, body))
                return {
                    "status": 201,
                    "data": {
                        "access_token": "new-token",
                        "refresh_token": "new-refresh-token",
                        "expires_in": 1500,
                    },
                }

        with patch.object(gopay_cycle, "GopayClient", FakeClient), \
                patch.object(gopay_cycle, "save_state", lambda target: None):
            result = gopay_cycle.check_token_valid(state)

        self.assertTrue(result["success"])
        self.assertTrue(result["token_valid"])
        self.assertTrue(result["refreshed"])
        self.assertTrue(result["has_min_balance"])
        self.assertEqual(state["token"], "new-token")
        self.assertEqual(state["refresh_token"], "new-refresh-token")
        self.assertEqual([call[0] for call in calls], ["get", "post", "get", "get"])

    def test_check_gopay_balance_uses_wallet_not_debit(self):
        state = {"token": "access-token", "device": {}}

        class FakeClient:
            def __init__(self, token, proxy=None, device=None):
                pass

            def get(self, url, **kwargs):
                return {"status": 200, "data": [
                    {"type": "DEBIT", "balance": {"value": 1, "currency": "IDR"}},
                    {"type": "GOPAY_WALLET", "balance": {"value": 0, "currency": "IDR"}},
                ], "raw": {"success": True}}

        with patch.object(gopay_cycle, "GopayClient", FakeClient), \
                patch.object(gopay_cycle, "save_state", lambda target: None):
            result = gopay_cycle.check_gopay_balance(state)

        self.assertTrue(result["success"])
        self.assertFalse(result["has_min_balance"])
        self.assertEqual(result["balance_amount"], 0)
        self.assertEqual(state["last_error"], "INSUFFICIENT_GOPAY_BALANCE")

    def test_login_pending_expires_without_timestamp(self):
        state = {
            "stage": "login_otp_pending",
            "_login_otp_token": "otp-token",
            "_login_2fa_token": "2fa-token",
        }

        self.assertTrue(gopay_cycle.expire_login_if_needed(state, now=1000))
        self.assertEqual(state["stage"], "idle")
        self.assertNotIn("_login_otp_token", state)
        self.assertEqual(state["last_error"], "LOGIN_OTP_TIMEOUT")

    def test_login_pending_uses_expiry_timestamp(self):
        state = {
            "stage": "login_otp_pending",
            "_login_otp_token": "otp-token",
            "_login_2fa_token": "2fa-token",
            "_login_otp_expires_at": 2000,
        }

        self.assertFalse(gopay_cycle.expire_login_if_needed(state, now=1999))
        self.assertTrue(gopay_cycle.expire_login_if_needed(state, now=2000))
        self.assertEqual(state["stage"], "idle")

    def test_signup_pending_uses_expiry_timestamp(self):
        state = {
            "stage": "signup_otp_pending",
            "_signup_phone": TEST_LOCAL_PHONE,
            "_signup_otp_token": "otp-token",
            "_signup_otp_expires_at": 2000,
        }

        self.assertFalse(gopay_cycle.expire_signup_if_needed(state, now=1999))
        self.assertTrue(gopay_cycle.expire_signup_if_needed(state, now=2000))
        self.assertEqual(state["stage"], "idle")
        self.assertNotIn("_signup_otp_token", state)
        self.assertEqual(state["last_error"], "SIGNUP_OTP_TIMEOUT")

    def test_signup_pin_pending_uses_expiry_timestamp(self):
        state = {
            "stage": "signup_pin_otp_pending",
            "token": "token",
            "_signup_pin_otp_token": "otp-token",
            "_signup_pin_otp_expires_at": 2000,
        }

        self.assertFalse(gopay_cycle.expire_signup_if_needed(state, now=1999))
        self.assertTrue(gopay_cycle.expire_signup_if_needed(state, now=2000))
        self.assertEqual(state["stage"], "signup_pin_required")
        self.assertNotIn("_signup_pin_otp_token", state)
        self.assertEqual(state["last_error"], "SIGNUP_PIN_OTP_TIMEOUT")


class LogonProfileTests(unittest.TestCase):
    def test_new_logon_device_profile_randomizes_model(self):
        with patch.object(gopay_cycle, "generate_device_fingerprint", return_value={"x-uniqueid": "u1"}) as gen:
            profile = gopay_cycle.new_logon_device_profile()

        gen.assert_called_once_with(randomize_model=True)
        self.assertEqual(profile["x-uniqueid"], "u1")
        self.assertTrue(profile["profile_id"])
        self.assertTrue(profile["profile_created_at"])

    def test_start_login_uses_fresh_profile_each_start(self):
        states = [{}, {}]
        profiles = [
            {"profile_id": "p1", "x-uniqueid": "u1"},
            {"profile_id": "p2", "x-uniqueid": "u2"},
        ]
        client_devices = []

        class FakeClient:
            def __init__(self, token, proxy=None, device=None):
                client_devices.append(device)

            def post(self, url, body=None, **kwargs):
                return {
                    "status": 401,
                    "data": {},
                    "raw": {
                        "errors": [{
                            "message": f"Could not find the user {TEST_E164_PHONE}",
                            "message_title": "Invalid User",
                        }],
                    },
                }

        with patch.object(gopay_cycle, "save_state", lambda target: None), \
                patch.object(gopay_cycle, "new_logon_device_profile", side_effect=profiles), \
                patch.object(gopay_cycle, "GopayClient", FakeClient):
            first = gopay_cycle.start_login(states[0], TEST_LOCAL_PHONE, TEST_PIN, "+62")
            second = gopay_cycle.start_login(states[1], TEST_LOCAL_PHONE, TEST_PIN, "+62")

        self.assertTrue(first["not_registered"])
        self.assertTrue(second["not_registered"])
        self.assertEqual(states[0]["device"]["profile_id"], "p1")
        self.assertEqual(states[1]["device"]["profile_id"], "p2")
        self.assertEqual(client_devices[0]["profile_id"], "p1")
        self.assertEqual(client_devices[1]["profile_id"], "p2")


class ProfileHeaderTests(unittest.TestCase):
    def test_customer_slim_get_headers_match_gopay_capture_shape(self):
        device = gopay_cycle.new_logon_device_profile()
        client = gopay_client.GopayClient("access-token", device=device)

        with patch.object(gopay_client, "HMAC_KEY", "test-key"):
            header_sets = [
                client._headers("GET", url, "", None)
                for url in (
                    "https://customer.gopayapi.com/v1/users/profile",
                    "https://customer.gopayapi.com/v1/payment-options/balances",
                )
            ]

        for headers in header_sets:
            lower = {key.lower(): value for key, value in headers.items()}
            for key in (
                "authorization",
                "x-appversion",
                "x-uniqueid",
                "x-phonemake",
                "x-e1",
                "x-deviceos",
                "x-user-type",
                "user-agent",
                "x-appid",
                "x-apptype",
                "x-e2",
                "x-m1",
                "x-phonemodel",
                "x-platform",
                "accept-language",
                "x-user-locale",
                "x-devicetoken",
                "gojek-country-code",
            ):
                self.assertIn(key, lower)
            for key in (
                "x-e3",
                "d1",
                "x-session-id",
                "adjts",
                "x-pushtokentype",
                "user-uuid",
                "x-location",
                "x-location-accuracy",
                "x-dark-mode",
                "content-type",
            ):
                self.assertNotIn(key, lower)

    def test_gojek_activity_change_phone_headers_match_capture_shape(self):
        device = gopay_cycle.new_logon_device_profile()
        client = gopay_client.GopayClient("access-token", device=device)
        body = json.dumps({
            "email": "a@aa.cc",
            "name": "gg",
            "phone": "+6289600000000",
            "profile_image_url": None,
        }, separators=(",", ":"))

        with patch.object(gopay_client, "HMAC_KEY", "test-key"):
            headers = client._headers(
                "PATCH",
                "https://api.gojekapi.com/v5/customers",
                body,
                {"pin": "000000"},
            )

        lower = {key.lower(): value for key, value in headers.items()}
        for key in (
            "accept-encoding",
            "authorization",
            "gojek-service-area",
            "country-code",
            "x-appversion",
            "x-m1",
            "gojek-country-code",
            "x-uniqueid",
            "x-phonemake",
            "x-help-version",
            "x-e1",
            "x-deviceos",
            "x-user-type",
            "user-agent",
            "x-appid",
            "gojek-timezone",
            "content-type",
            "x-apptype",
            "x-user-locale",
            "x-devicetoken",
            "x-e2",
            "accept-language",
            "x-phonemodel",
            "x-platform",
            "pin",
        ):
            self.assertIn(key, lower)
        for key in (
            "x-e3",
            "d1",
            "x-session-id",
            "adjts",
            "x-pushtokentype",
            "user-uuid",
            "x-location",
            "x-location-accuracy",
            "x-dark-mode",
            "x-authsdk-version",
            "transaction-id",
        ):
            self.assertNotIn(key, lower)


class ReplayTests(unittest.TestCase):
    def test_build_qris_payment_body_from_raw_qr(self):
        merchant_account = replay.encode_tlv(
            [
                ("00", "COM.EXAMPLE.QRIS"),
                ("01", "123456780000000001"),
                ("02", "MERCHANT01"),
                ("03", "UME"),
            ]
        )
        additional = replay.encode_tlv([("07", "TERM1"), ("50", "ORDER123")])
        qr = replay.encode_tlv(
            [
                ("00", "01"),
                ("01", "12"),
                ("26", merchant_account),
                ("52", "5817"),
                ("53", "360"),
                ("54", "1"),
                ("58", "ID"),
                ("59", "Example Shop"),
                ("60", "JAKARTA"),
                ("61", "12345"),
                ("62", additional),
                ("63", "ABCD"),
            ]
        )

        body = replay.build_qris_payment_body(qr)

        self.assertEqual(body["amount"], {"value": 1, "currency": "IDR"})
        self.assertEqual(body["channel_type"], "DYNAMIC_QR")
        info = body["additional_data"]["aspiqr_information"]
        self.assertEqual(info["merchant_id"], "MERCHANT01")
        self.assertEqual(
            info["additional_data_national"],
            replay.build_additional_data_national("12345", replay.parse_emv_tlv(additional)),
        )

    def test_run_qr_payment_uses_order_and_pin_without_files(self):
        calls = []
        case = self

        class FakeClient:
            def post(self, url, body=None, extra_headers=None, **kwargs):
                calls.append(("post", url, body, extra_headers or {}))
                if url.endswith("/checkout/list"):
                    return {
                        "status": 200,
                        "data": {"selected_options": [{"token": "payment-token"}]},
                        "raw": {"success": True},
                    }
                if url.endswith("/pin/tokens"):
                    case.assertEqual(body["pin"], TEST_PIN)
                    return {"status": 200, "data": {"token": "pin-token"}, "raw": {"success": True}}
                raise AssertionError(f"unexpected post {url}")

            def put(self, url, body=None, **kwargs):
                calls.append(("put", url, body, {}))
                return {"status": 200, "data": {}, "raw": {"success": True}}

            def get(self, url, **kwargs):
                calls.append(("get", url, None, {}))
                return {"status": 200, "data": {}, "raw": {"success": True}}

            def patch(self, url, body=None, extra_headers=None, **kwargs):
                calls.append(("patch", url, body, extra_headers or {}))
                if body.get("challenge") is None:
                    return {
                        "status": 461,
                        "data": {
                            "challenge": {
                                "action": {"value": {"challenge_id": "challenge-id", "client_id": "client-id"}},
                                "metadata": {"challenge_id": "challenge-id", "client_id": "client-id"},
                            }
                        },
                        "raw": {"success": False},
                    }
                case.assertEqual(body["challenge"]["value"]["pin_token"], "pin-token")
                return {"status": 200, "data": {"payment_id": "pay-1", "status": "PAID"}, "raw": {"success": True}}

        order = {
            "payment_id": "pay-1",
            "amount": {"value": 1, "currency": "IDR"},
            "payment_widget_metadata": {"service_id": "1001", "merchant_id": "MERCHANT01"},
            "additional_data": {"merchant_information": {"id": "MERCHANT01"}},
            "metadata": {},
            "checksum": {"version": "3", "value": "1"},
            "channel_type": "ONLINE_GATEWAY",
        }

        result = replay.run_qr_payment(
            FakeClient(),
            replay.QrPaymentOptions(order_json=json.dumps(order), pin=TEST_PIN),
        )

        self.assertTrue(result["success"], result.get("error_message"))
        self.assertEqual(result["payment_id"], "pay-1")
        self.assertEqual([call[0] for call in calls], ["post", "put", "patch", "get", "post", "patch"])
        self.assertEqual([step["label"] for step in result["steps"]], [
            "checkout_list", "last_used", "capture1", "pin_page", "pin_tokens", "capture2",
        ])

    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_replay_qr_rpc_uses_state_token(self):
        returned = {
            "success": True,
            "error_message": "",
            "payment_id": "pay-1",
            "status": "PAID",
            "steps": [{"label": "capture2", "status_code": 200}],
        }
        state = {"stage": "ready", "token": "state-token", "device": {}}
        fake_client = object()
        with patch.object(cycle_server, "_client", return_value=fake_client) as make_client, \
                patch.object(cycle_server, "run_qr_payment", return_value=returned) as run:
            resp = cycle_server.GopayCycleServicer().ReplayQrPayment(
                cycle_server.gopay_cycle_pb2.ReplayQrPaymentRequest(
                    order_json=json.dumps({"payment_id": "pay-1"}),
                    pin=TEST_PIN,
                    state_json=json.dumps(state),
                ),
                None,
            )

        self.assertTrue(resp.success)
        self.assertEqual(resp.payment_id, "pay-1")
        self.assertEqual(json.loads(resp.state_json)["token"], "state-token")
        make_client.assert_called_once()
        self.assertIs(run.call_args.args[0], fake_client)


class LoginStartTests(unittest.TestCase):
    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_consumed_stage_uses_valid_token_without_otp(self):
        state = {
            "stage": "consumed",
            "token": jwt_with_exp(int(time.time()) + 3600),
            "refresh_token": "refresh-token",
        }

        def fake_load_state():
            return dict(state)

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_check_token_valid(target):
            target["stage"] = "ready"
            state["stage"] = "ready"
            return {
                "success": True,
                "token_valid": True,
                "has_min_balance": True,
                "balance_amount": 1,
            }

        with patch.object(cycle_server, "load_state", fake_load_state), \
                patch.object(cycle_server, "save_state", fake_save_state), \
                patch.object(cycle_server, "check_token_valid", fake_check_token_valid), \
                patch.object(cycle_server, "access_token_usable", lambda target, min_ttl=30: True):
            resp = cycle_server.GopayCycleServicer().LoginStart(
                cycle_server.gopay_cycle_pb2.LoginStartRequest(),
                None,
            )

        self.assertTrue(resp.success)
        self.assertFalse(resp.otp_sent)
        self.assertEqual(state["stage"], "ready")

    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_status_expires_stale_login_otp_pending(self):
        state = {
            "stage": "login_otp_pending",
            "_login_phone": TEST_LOCAL_PHONE,
            "_login_otp_token": "otp-token",
            "_login_2fa_token": "2fa-token",
        }

        def fake_load_state():
            return dict(state)

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        with patch.object(cycle_server, "load_state", fake_load_state), \
                patch.object(cycle_server, "save_state", fake_save_state):
            resp = cycle_server.GopayCycleServicer().Status(
                cycle_server.gopay_cycle_pb2.StatusRequest(),
                None,
            )

        self.assertEqual(resp.stage, "idle")
        self.assertFalse(resp.token_present)
        self.assertEqual(resp.error_message, "LOGIN_OTP_TIMEOUT")
        self.assertNotIn("_login_otp_token", state)

    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_auth_start_returns_ready_when_token_valid(self):
        state = {"stage": "ready", "token": "access-token"}

        def fake_load_state():
            return dict(state)

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        with patch.object(cycle_server, "load_state", fake_load_state), \
                patch.object(cycle_server, "save_state", fake_save_state), \
                patch.object(cycle_server, "check_token_valid", lambda target: {"success": True, "token_valid": True, "has_min_balance": True, "balance_amount": 1}), \
                patch.object(cycle_server, "start_login") as start_login:
            resp = cycle_server.GopayCycleServicer().AuthStart(
                cycle_server.gopay_cycle_pb2.AuthStartRequest(),
                None,
            )

        self.assertTrue(resp.success)
        self.assertTrue(resp.ready)
        self.assertEqual(resp.mode, "token")
        start_login.assert_not_called()

    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_auth_start_accepts_valid_token_without_min_balance(self):
        state = {"stage": "ready", "token": "access-token", "balance_amount": 0, "balance_currency": "IDR"}

        def fake_load_state():
            return dict(state)

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        with patch.object(cycle_server, "load_state", fake_load_state), \
                patch.object(cycle_server, "save_state", fake_save_state), \
                patch.object(cycle_server, "check_token_valid", lambda target: {
                    "success": True,
                    "token_valid": True,
                    "has_min_balance": False,
                    "balance_amount": 0,
                    "balance_currency": "IDR",
                }), \
                patch.object(cycle_server, "start_login") as start_login:
            resp = cycle_server.GopayCycleServicer().AuthStart(
                cycle_server.gopay_cycle_pb2.AuthStartRequest(),
                None,
            )

        self.assertTrue(resp.success)
        self.assertTrue(resp.ready)
        self.assertEqual(resp.mode, "token")
        self.assertEqual(resp.error_message, "")
        start_login.assert_not_called()

    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_auth_start_uses_configured_main_phone_for_login_probe(self):
        state = {"stage": "idle", "phone": TEST_CHANGE_LOCAL_PHONE}
        captured = {}

        def fake_load_state():
            return dict(state)

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_start_login(target, phone, pin, country_code):
            captured["phone"] = phone
            captured["country_code"] = country_code
            return {
                "success": True,
                "ready": False,
                "otp_sent": True,
                "verification_id": "verification-id",
                "method": "otp_wa",
            }

        with patch.object(cycle_server, "load_state", fake_load_state), \
                patch.object(cycle_server, "save_state", fake_save_state), \
                patch.object(cycle_server, "MAIN_PHONE", TEST_FULL_PHONE), \
                patch.object(cycle_server, "check_token_valid", lambda target: {"success": False, "token_valid": False}), \
                patch.object(cycle_server, "start_login", fake_start_login):
            resp = cycle_server.GopayCycleServicer().AuthStart(
                cycle_server.gopay_cycle_pb2.AuthStartRequest(phone=TEST_CHANGE_LOCAL_PHONE),
                None,
            )

        self.assertTrue(resp.success)
        self.assertEqual(resp.mode, "login")
        self.assertEqual(captured["phone"], TEST_LOCAL_PHONE)
        self.assertEqual(captured["country_code"], "+62")

    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_signup_start_uses_configured_main_phone(self):
        state = {"stage": "idle", "phone": TEST_CHANGE_LOCAL_PHONE}
        captured = {}

        def fake_load_state():
            return dict(state)

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        def fake_start_signup(target, phone, name, email, country_code):
            captured["phone"] = phone
            captured["name"] = name
            captured["email"] = email
            captured["country_code"] = country_code
            return {
                "success": True,
                "otp_sent": True,
                "verification_id": "verification-id",
                "method": "otp_wa",
            }

        with patch.object(cycle_server, "load_state", fake_load_state), \
                patch.object(cycle_server, "save_state", fake_save_state), \
                patch.object(cycle_server, "MAIN_PHONE", TEST_LOCAL_PHONE), \
                patch.object(cycle_server, "start_signup", fake_start_signup):
            resp = cycle_server.GopayCycleServicer().SignupStart(
                cycle_server.gopay_cycle_pb2.SignupStartRequest(phone=TEST_CHANGE_LOCAL_PHONE),
                None,
            )

        self.assertTrue(resp.success)
        self.assertEqual(captured["phone"], TEST_LOCAL_PHONE)
        self.assertEqual(captured["country_code"], "+62")


class SignupFlowTests(unittest.TestCase):
    def test_signup_basic_authorization_uses_env_uuid(self):
        request_id = "87654321-4321-6789-4321-678987654321"
        expected = "Basic " + base64.b64encode(request_id.encode("utf-8")).decode("ascii")

        with patch.dict(gopay_cycle.os.environ, {"GOPAY_SIGNUP_AUTH_UUID": request_id}, clear=False), \
                patch.object(gopay_cycle.uuid, "uuid4", side_effect=AssertionError("uuid4 should not be called")):
            self.assertEqual(gopay_cycle._signup_basic_authorization(), expected)

    def test_start_signup_uses_signup_cvs_flow(self):
        state = {}
        calls = []
        client_devices = []
        profile = {"profile_id": "signup-profile", "x-uniqueid": "signup-u1"}

        class FakeClient:
            def __init__(self, token, proxy=None, device=None):
                self.token = token
                client_devices.append(device)

            def post(self, url, body=None, **kwargs):
                calls.append((url, body, kwargs))
                if url.endswith("/cvs/v1/methods"):
                    return {
                        "status": 200,
                        "data": {
                            "default_method": "otp_wa",
                            "methods": ["otp_wa", "otp_sms"],
                            "verification_id": "verification-id",
                        },
                    }
                if url.endswith("/cvs/v1/initiate"):
                    return {"status": 200, "data": {"otp_token": "signup-otp-token"}}
                raise AssertionError(f"unexpected post {url}")

        with patch.object(gopay_cycle, "save_state", lambda target: None), \
                patch.object(gopay_cycle, "new_logon_device_profile", return_value=profile), \
                patch.object(gopay_cycle, "GopayClient", FakeClient):
            result = gopay_cycle.start_signup(
                state,
                phone=TEST_FULL_PHONE,
                name="gg",
                email="gg@example.test",
                country_code="+62",
            )

        self.assertTrue(result["success"])
        self.assertEqual(state["stage"], "signup_otp_pending")
        self.assertEqual(state["_signup_phone"], TEST_LOCAL_PHONE)
        self.assertEqual(state["_signup_otp_token"], "signup-otp-token")
        self.assertEqual(state["device"]["profile_id"], "signup-profile")
        self.assertEqual(client_devices[0]["profile_id"], "signup-profile")
        self.assertEqual(calls[0][1]["flow"], "signup")
        self.assertEqual(calls[0][1]["country_code"], "+62")
        self.assertEqual(calls[1][1]["verification_method"], "otp_wa")

    def test_complete_signup_creates_customer_and_refreshes_token(self):
        state = {
            "stage": "signup_otp_pending",
            "token": "old-token",
            "device": {},
            "_signup_phone": TEST_LOCAL_PHONE,
            "_signup_country_code": "+62",
            "_signup_name": "gg",
            "_signup_email": "gg@example.test",
            "_signup_verification_id": "verification-id",
            "_signup_verification_method": "otp_wa",
            "_signup_otp_token": "signup-otp-token",
        }
        calls = []

        class FakeClient:
            def __init__(self, token, proxy=None, device=None):
                self.token = token

            def post(self, url, body=None, **kwargs):
                calls.append((url, body, kwargs))
                if url.endswith("/cvs/v1/verify"):
                    return {"status": 200, "data": {"verification_token": "verification-token"}}
                if url.endswith("/v7/customers/signup"):
                    return {
                        "status": 201,
                        "data": {
                            "access_token": jwt_with_exp(int(time.time()) + 60),
                            "refresh_token": "refresh-token",
                            "expires_in": 1500,
                        },
                    }
                raise AssertionError(f"unexpected post {url}")

        def fake_refresh(target, min_ttl_seconds=None, force=False):
            target["token"] = jwt_with_exp(int(time.time()) + 3600)
            return {"success": True, "refreshed": True}

        with patch.object(gopay_cycle, "save_state", lambda target: None), \
                patch.object(gopay_cycle, "GopayClient", FakeClient), \
                patch.object(gopay_cycle, "ensure_access_token", fake_refresh), \
                patch.dict(gopay_cycle.os.environ, {"GOPAY_SIGNUP_AUTH_UUID": ""}, clear=False), \
                patch.object(
                    gopay_cycle.uuid,
                    "uuid4",
                    return_value=gopay_cycle.uuid.UUID("12345678-1234-5678-1234-567812345678"),
                ):
            result = gopay_cycle.complete_signup(state, "1234")

        self.assertTrue(result["success"])
        self.assertEqual(state["stage"], "signup_pin_required")
        self.assertEqual(state["phone"], TEST_LOCAL_PHONE)
        self.assertNotIn("_signup_otp_token", state)
        signup_call = calls[1]
        self.assertEqual(signup_call[1]["data"]["phone"], TEST_E164_PHONE)
        self.assertEqual(
            signup_call[2]["extra_headers"],
            {
                "Authorization": "Basic MTIzNDU2NzgtMTIzNC01Njc4LTEyMzQtNTY3ODEyMzQ1Njc4",
                "Verification-Token": "Bearer verification-token",
            },
        )

    def test_signup_pin_start_and_complete_use_pin_setup_flow(self):
        state = {
            "stage": "signup_pin_required",
            "token": jwt_with_exp(int(time.time()) + 3600),
            "phone": TEST_LOCAL_PHONE,
            "device": {},
        }
        calls = []

        class FakeClient:
            def __init__(self, token, proxy=None, device=None):
                self.token = token

            def post(self, url, body=None, **kwargs):
                calls.append((url, body, kwargs))
                if url.endswith("/api/v1/users/pins/allowed"):
                    return {"status": 200, "data": {"success": True}}
                if url.endswith("/api/v1/users/pin/challenges"):
                    return {
                        "status": 200,
                        "data": {"challenge_id": "challenge-id", "client_id": "pin-client-id"},
                    }
                if url.endswith("/cvs/v1/methods"):
                    return {
                        "status": 200,
                        "data": {
                            "default_method": "otp_wa",
                            "methods": ["otp_wa", "otp_sms"],
                            "verification_id": "pin-verification-id",
                        },
                    }
                if url.endswith("/cvs/v1/initiate"):
                    return {"status": 200, "data": {"otp_token": "pin-otp-token"}}
                if url.endswith("/cvs/v1/verify"):
                    return {"status": 200, "data": {"verification_token": "pin-verification-token"}}
                if url.endswith("/api/v2/users/pins/setup/tokens"):
                    return {"status": 200, "data": {"token": "pin-token"}}
                raise AssertionError(f"unexpected post {url}")

        with patch.object(gopay_cycle, "save_state", lambda target: None), \
                patch.object(gopay_cycle, "GopayClient", FakeClient), \
                patch.object(gopay_cycle, "ensure_access_token", lambda target, **kwargs: {"success": True}):
            start = gopay_cycle.start_signup_pin(state, TEST_PIN)
            complete = gopay_cycle.complete_signup_pin(state, "1234", TEST_PIN)

        self.assertTrue(start["success"])
        self.assertTrue(complete["success"])
        self.assertEqual(state["stage"], "ready")
        self.assertEqual(state["phone"], TEST_LOCAL_PHONE)
        self.assertNotIn("_signup_pin_otp_token", state)
        methods_call = calls[2]
        self.assertEqual(methods_call[1]["flow"], "goto_pin_wa_sms_gp_app")
        self.assertIsNone(methods_call[1]["country_code"])
        setup_call = calls[-1]
        self.assertEqual(setup_call[1], {
            "pin": TEST_PIN,
            "client_id": "pin-client-id",
            "challenge_id": "challenge-id",
        })
        self.assertEqual(
            setup_call[2]["extra_headers"],
            {"Verification-Token": "Bearer pin-verification-token"},
        )


class DeactivationFlowTests(unittest.TestCase):
    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_deactivate_start_skips_pin_for_account_without_pin_and_accepts_otp_required(self):
        state = {
            "_tmp_token": jwt_with_exp(int(time.time()) + 3600),
            "device": {},
        }
        calls = []

        class FakeClient:
            def __init__(self, token, proxy=None, device=None):
                self.token = token

            def get(self, url, **kwargs):
                calls.append(("get", url))
                if url.endswith("/v1/users/profile"):
                    return {"status": 200, "data": {"is_pin_setup": False}}
                if url.endswith("/api/v1/users/deactivate/check"):
                    return {
                        "status": 462,
                        "data": {},
                        "raw": {"errors": [{"code": "GoPay-1603", "message": "You must enter OTP to continue"}]},
                    }
                raise AssertionError(f"unexpected get {url}")

            def post(self, url, body=None, **kwargs):
                raise AssertionError(f"pin endpoint should not be called: {url}")

        with patch.object(cycle_server, "load_state", lambda: state), \
                patch.object(cycle_server, "save_state", lambda target: None), \
                patch.object(cycle_server, "GopayClient", FakeClient), \
                patch.object(cycle_server, "GOPAY_PIN", ""):
            resp = cycle_server.GopayCycleServicer().DeactivateStart(
                cycle_server.gopay_cycle_pb2.DeactivateStartRequest(),
                None,
            )

        self.assertTrue(resp.success)
        self.assertTrue(resp.otp_sent)
        self.assertEqual(state["stage"], "deactivate_otp_pending")
        self.assertEqual(calls, [
            ("get", "https://customer.gopayapi.com/v1/users/profile"),
            ("get", "https://customer.gopayapi.com/api/v1/users/deactivate/check"),
        ])

    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_deactivate_complete_uses_captured_reason_and_clears_tmp_token(self):
        state = {
            "stage": "deactivate_otp_pending",
            "_tmp_token": jwt_with_exp(int(time.time()) + 3600),
            "_tmp_refresh_token": "refresh-token",
            "device": {},
        }
        bodies = []

        class FakeClient:
            def __init__(self, token, proxy=None, device=None):
                self.token = token

            def delete(self, url, body=None, **kwargs):
                bodies.append((url, body))
                return {"status": 200, "data": {"success": True}}

        with patch.object(cycle_server, "load_state", lambda: state), \
                patch.object(cycle_server, "save_state", lambda target: None), \
                patch.object(cycle_server, "GopayClient", FakeClient):
            resp = cycle_server.GopayCycleServicer().DeactivateComplete(
                cycle_server.gopay_cycle_pb2.DeactivateCompleteRequest(otp="1234"),
                None,
            )

        self.assertTrue(resp.success)
        self.assertEqual(state["stage"], "deactivated")
        self.assertNotIn("_tmp_token", state)
        self.assertEqual(bodies, [(
            "https://customer.gopayapi.com/api/v1/users/deactivate",
            {
                "otp": "1234",
                "reason": "I no longer need digital payment services",
                "description": None,
            },
        )])


class ChangePhoneActivityFlowTests(unittest.TestCase):
    def test_check_phone_login_methods_uses_fresh_fingerprint_per_call(self):
        devices = [{"profile_id": "p1"}, {"profile_id": "p2"}]
        used_devices = []

        class FakeClient:
            def __init__(self, token, proxy=None, device=None):
                used_devices.append(device)

            def post(self, url, body=None, **kwargs):
                return {
                    "status": 401,
                    "raw": {
                        "errors": [{
                            "message_title": "Invalid User",
                            "message": "Could not find the user +628000000000",
                        }]
                    },
                }

        with patch.object(gopay_cycle, "new_logon_device_profile", side_effect=devices), \
                patch.object(gopay_cycle, "GopayClient", FakeClient):
            first = gopay_cycle.check_phone_by_login_methods("8000000000", "+62")
            second = gopay_cycle.check_phone_by_login_methods("8000000001", "+62")

        self.assertTrue(first["available"])
        self.assertTrue(second["available"])
        self.assertEqual(used_devices, devices)

    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_check_phone_returns_registered_from_login_methods(self):
        with patch.object(cycle_server, "load_state", side_effect=AssertionError("state should not be loaded")), \
                patch.object(cycle_server, "save_state", side_effect=AssertionError("state should not be saved")), \
                patch.object(cycle_server, "check_phone_by_login_methods", return_value={
                    "success": True,
                    "available": False,
                    "status": "registered",
                }) as check:
            resp = cycle_server.GopayCycleServicer().CheckPhone(
                cycle_server.gopay_cycle_pb2.CheckPhoneRequest(phone="8000000000"),
                None,
            )

        self.assertFalse(resp.available)
        self.assertEqual(resp.status, "registered")
        self.assertEqual(resp.error_message, "PHONE_REGISTERED")
        check.assert_called_once_with("8000000000", "+62")

    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_check_phone_returns_available_without_prechecking_change_phone(self):
        with patch.object(cycle_server, "load_state", side_effect=AssertionError("state should not be loaded")), \
                patch.object(cycle_server, "save_state", side_effect=AssertionError("state should not be saved")), \
                patch.object(cycle_server, "check_phone_by_login_methods", return_value={
                    "success": True,
                    "available": True,
                    "status": "available",
                }) as check:
            resp = cycle_server.GopayCycleServicer().CheckPhone(
                cycle_server.gopay_cycle_pb2.CheckPhoneRequest(phone="8000000000"),
                None,
            )

        self.assertTrue(resp.available)
        self.assertEqual(resp.status, "available")
        check.assert_called_once_with("8000000000", "+62")

    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_change_phone_start_uses_activity_v5_customers_flow(self):
        state = {
            "stage": "ready",
            "token": jwt_with_exp(int(time.time()) + 3600),
            "email": "a@aa.cc",
            "name": "gg",
            "device": {},
        }
        calls = []

        class FakeClient:
            def __init__(self, token, proxy=None, device=None):
                self.token = token

            def post(self, url, body=None, **kwargs):
                calls.append(("post", url, body, kwargs))
                if "account-recovery/account-list" in url:
                    return {"status": 200, "data": {"accounts": []}}
                raise AssertionError(f"unexpected post {url}")

            def patch(self, url, body=None, extra_headers=None, **kwargs):
                calls.append(("patch", url, body, extra_headers or {}))
                if not extra_headers:
                    return {"status": 461, "data": {"errors": [{"code": "CO:CUST:pin_verification"}]}}
                return {
                    "status": 200,
                    "data": {
                        "code": "CO:CUST:sms_verification",
                        "otp_token": "otp-token",
                        "expires_in": 300,
                    },
                }

        def fake_load_state():
            return dict(state)

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        with patch.object(cycle_server, "load_state", fake_load_state), \
                patch.object(cycle_server, "save_state", fake_save_state), \
                patch.object(cycle_server, "GopayClient", FakeClient), \
                patch.object(cycle_server, "ensure_access_token", lambda target: {"success": True}), \
                patch.object(cycle_server, "access_token_usable", lambda target, min_ttl=30: True):
            resp = cycle_server.GopayCycleServicer().ChangePhoneStart(
                cycle_server.gopay_cycle_pb2.ChangePhoneStartRequest(pin=TEST_PIN, new_phone=TEST_CHANGE_FULL_PHONE),
                None,
            )

        self.assertTrue(resp.success)
        self.assertEqual(resp.new_phone, TEST_CHANGE_LOCAL_PHONE)
        self.assertEqual(state["_change_otp_token"], "otp-token")
        patch_urls = [call[1] for call in calls if call[0] == "patch"]
        self.assertEqual(patch_urls, [
            "https://api.gojekapi.com/v5/customers",
            "https://api.gojekapi.com/v5/customers",
        ])

    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_change_phone_retry_uses_activity_otp_retry(self):
        state = {
            "stage": "change_phone_otp_pending",
            "token": jwt_with_exp(int(time.time()) + 3600),
            "_change_phone": "89611122227",
            "_change_otp_token": "old-token",
            "device": {},
        }
        calls = []

        class FakeClient:
            def __init__(self, token, proxy=None, device=None):
                pass

            def post(self, url, body=None, **kwargs):
                calls.append((url, body))
                return {"status": 200, "data": {"otp_token": "new-token", "otp_expires_in": 300}}

        def fake_load_state():
            return dict(state)

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        with patch.object(cycle_server, "load_state", fake_load_state), \
                patch.object(cycle_server, "save_state", fake_save_state), \
                patch.object(cycle_server, "GopayClient", FakeClient), \
                patch.object(cycle_server, "ensure_access_token", lambda target: {"success": True}), \
                patch.object(cycle_server, "access_token_usable", lambda target, min_ttl=30: True):
            resp = cycle_server.GopayCycleServicer().ChangePhoneRetry(
                cycle_server.gopay_cycle_pb2.ChangePhoneRetryRequest(),
                None,
            )

        self.assertTrue(resp.success)
        self.assertEqual(calls, [("https://api.gojekapi.com/v2/otp/retry", {
            "otp_token": "old-token",
            "channel_type": "sms",
        })])
        self.assertEqual(state["_change_otp_token"], "new-token")

    @unittest.skipIf(cycle_server is None, f"cycle_server import failed: {CYCLE_SERVER_IMPORT_ERROR}")
    def test_change_phone_complete_uses_activity_v5_verify_without_country_sync_by_default(self):
        state = {
            "stage": "change_phone_otp_pending",
            "token": jwt_with_exp(int(time.time()) + 3600),
            "refresh_token": "seed-refresh-token",
            "token_expires_at": int(time.time()) + 3600,
            "_change_phone": "89611122227",
            "_change_otp_token": "otp-token",
            "device": {},
        }
        calls = []

        class FakeClient:
            def __init__(self, token, proxy=None, device=None):
                pass

            def post(self, url, body=None, **kwargs):
                calls.append(("post", url, body))
                return {"status": 200, "data": {"message": "Phone verification was successful!"}}

            def put(self, url, body=None, **kwargs):
                calls.append(("put", url, body))
                return {"status": 200, "data": {}}

        def fake_load_state():
            return dict(state)

        def fake_save_state(next_state):
            state.clear()
            state.update(next_state)

        with patch.object(cycle_server, "load_state", fake_load_state), \
                patch.object(cycle_server, "save_state", fake_save_state), \
                patch.object(cycle_server, "GopayClient", FakeClient), \
                patch.object(cycle_server, "GOPAY_CHANGE_PHONE_COUNTRY_SYNC", False), \
                patch.object(cycle_server, "ensure_access_token", lambda target: {"success": True}), \
                patch.object(cycle_server, "access_token_usable", lambda target, min_ttl=30: True):
            resp = cycle_server.GopayCycleServicer().ChangePhoneComplete(
                cycle_server.gopay_cycle_pb2.ChangePhoneCompleteRequest(otp="1234"),
                None,
            )

        self.assertTrue(resp.success)
        self.assertEqual(calls, [("post", "https://api.gojekapi.com/v5/customers/verificationUpdateProfile", {
            "otp": "1234",
            "otp_token": "otp-token",
        })])
        self.assertEqual(state["phone"], "89611122227")
        self.assertNotIn("token", state)
        self.assertNotIn("refresh_token", state)
        self.assertNotIn("token_expires_at", state)
        self.assertIn("_tmp_token", state)
        self.assertEqual(state["_tmp_refresh_token"], "seed-refresh-token")
        self.assertEqual(state["_tmp_phone"], "89611122227")
        self.assertNotIn("_change_otp_token", state)


if __name__ == "__main__":
    unittest.main()
