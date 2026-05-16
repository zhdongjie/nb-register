import unittest
from unittest.mock import patch

import payment_pb2
import payment_server
from payment_server import FlowStore, PaymentService


TEST_ACCOUNT_PHONE = "test-account-phone"
TEST_PIN = "000000"


class FakeCharger:
    def __init__(self):
        self.closed = False
        self.midtrans_tokenization = "true"
        self.pin = TEST_PIN

    def close(self):
        self.closed = True


class FlowStoreTests(unittest.TestCase):
    def test_flow_store_keeps_flow_until_pop(self):
        store = FlowStore()
        charger = FakeCharger()

        flow_id = store.put(charger, {"snap_token": "snap"})
        flow = store.pop(flow_id)

        self.assertIsNotNone(flow)
        self.assertIs(flow.charger, charger)
        self.assertEqual(flow.state["snap_token"], "snap")
        self.assertIsNone(store.pop(flow_id))

    def test_close_releases_unpopped_flows(self):
        store = FlowStore()
        charger = FakeCharger()

        store.put(charger, {"snap_token": "snap"})
        store.close()

        self.assertTrue(charger.closed)


class PaymentServiceTests(unittest.TestCase):
    def test_create_checkout_link_returns_url_without_probe_or_payment(self):
        captured = {}

        class FakeChatGPTSession:
            headers = {}

            def close(self):
                captured["session_closed"] = True

        class FakeGoPayCharger:
            def __init__(self, chatgpt_session, gopay_cfg, **kwargs):
                captured["gopay_cfg"] = dict(gopay_cfg)
                captured["charger_kwargs"] = dict(kwargs)
                self.checkout_url = "https://pay.openai.com/c/pay/cs_test_link"

            def _chatgpt_create_checkout(self):
                captured["create_checkout_called"] = True
                return "cs_test_link"

            def close(self):
                captured["charger_closed"] = True

        def fake_build_chatgpt_session(auth_cfg, proxy=None):
            captured["auth_cfg"] = dict(auth_cfg)
            captured["build_proxy"] = proxy
            return FakeChatGPTSession()

        svc = PaymentService({"fresh_checkout": {"auth": {}}, "gopay": {"country_code": "62", "phone_number": "", "pin": TEST_PIN}})

        with patch.object(payment_server, "_build_chatgpt_session", fake_build_chatgpt_session), \
                patch.object(payment_server, "resolve_checkout_proxy", return_value="socks5://checkout"), \
                patch.object(payment_server, "GoPayCharger", FakeGoPayCharger):
            resp = svc.CreateCheckoutLink(
                payment_pb2.CreateCheckoutLinkRequest(session_token="stale-session", access_token="test-token"),
                None,
            )

        self.assertTrue(resp.success)
        self.assertEqual(resp.checkout_session_id, "cs_test_link")
        self.assertEqual(resp.checkout_url, "https://pay.openai.com/c/pay/cs_test_link")
        self.assertEqual(captured["auth_cfg"]["access_token"], "test-token")
        self.assertNotIn("session_token", captured["auth_cfg"])
        self.assertEqual(captured["build_proxy"], "socks5://checkout")
        self.assertEqual(captured["charger_kwargs"]["checkout_proxy"], "socks5://checkout")
        self.assertTrue(captured["create_checkout_called"])
        self.assertTrue(captured["charger_closed"])

    def test_account_token_uses_supplied_token_and_phone(self):
        captured = {}

        class FakeChatGPTSession:
            headers = {}

            def close(self):
                pass

        class FakeGoPayCharger:
            def __init__(self, chatgpt_session, gopay_cfg, **kwargs):
                captured["gopay_cfg"] = dict(gopay_cfg)
                captured["charger_kwargs"] = dict(kwargs)

            def start_until_otp(self, stripe_pk="", billing=None, checkout_session_id="", checkout_url="", otp_channel=""):
                captured["checkout_session_id"] = checkout_session_id
                captured["checkout_url"] = checkout_url
                captured["otp_channel"] = otp_channel
                return {"snap_token": "snap", "issued_after_unix": 123}

            def close(self):
                pass

        def fake_build_chatgpt_session(auth_cfg, proxy=None):
            captured["auth_cfg"] = dict(auth_cfg)
            captured["build_proxy"] = proxy
            return FakeChatGPTSession()

        svc = PaymentService({"fresh_checkout": {"auth": {}}, "gopay": {"country_code": "62", "phone_number": "", "pin": TEST_PIN}})

        with patch.object(payment_server, "_build_chatgpt_session", fake_build_chatgpt_session), \
                patch.object(payment_server, "resolve_checkout_proxy", return_value="socks5://checkout"), \
                patch.object(payment_server, "resolve_payment_proxy", return_value="socks5://payment"), \
                patch.object(payment_server, "GoPayCharger", FakeGoPayCharger):
            resp = svc.StartGoPay(
                payment_pb2.StartGoPayRequest(
                    access_token="test-token",
                    use_account_token=True,
                    gopay_phone=TEST_ACCOUNT_PHONE,
                    otp_channel="sms",
                ),
                None,
            )

        self.assertTrue(resp.success)
        self.assertTrue(resp.otp_required)
        self.assertEqual(captured["auth_cfg"]["access_token"], "test-token")
        self.assertEqual(captured["gopay_cfg"]["phone_number"], TEST_ACCOUNT_PHONE)
        self.assertEqual(captured["build_proxy"], "socks5://checkout")
        self.assertEqual(captured["charger_kwargs"]["checkout_proxy"], "socks5://checkout")
        self.assertEqual(captured["charger_kwargs"]["payment_proxy"], "socks5://payment")
        self.assertEqual(captured["otp_channel"], "sms")

    def test_start_gopay_passes_tokenization_override(self):
        captured = {}

        class FakeChatGPTSession:
            headers = {}

            def close(self):
                pass

        class FakeGoPayCharger:
            def __init__(self, chatgpt_session, gopay_cfg, **kwargs):
                captured["gopay_cfg"] = dict(gopay_cfg)

            def start_until_otp(self, stripe_pk="", billing=None, checkout_session_id="", checkout_url="", otp_channel=""):
                captured["checkout_session_id"] = checkout_session_id
                captured["checkout_url"] = checkout_url
                return {"snap_token": "snap", "issued_after_unix": 123}

            def close(self):
                pass

        svc = PaymentService({"fresh_checkout": {"auth": {}}, "gopay": {"country_code": "62", "phone_number": "81234567890", "pin": TEST_PIN}})

        with patch.object(payment_server, "_build_chatgpt_session", return_value=FakeChatGPTSession()), \
                patch.object(payment_server, "resolve_checkout_proxy", return_value="socks5://checkout"), \
                patch.object(payment_server, "resolve_payment_proxy", return_value="socks5://payment"), \
                patch.object(payment_server, "GoPayCharger", FakeGoPayCharger):
            resp = svc.StartGoPay(
                payment_pb2.StartGoPayRequest(access_token="test-token", tokenization="false"),
                None,
            )

        self.assertTrue(resp.success)
        self.assertEqual(captured["gopay_cfg"]["tokenization"], "false")

    def test_prepare_then_start_prepared_gopay(self):
        captured = {}

        class FakeChatGPTSession:
            headers = {}

            def close(self):
                captured["session_closed"] = True

        class FakeGoPayCharger:
            def __init__(self, chatgpt_session, gopay_cfg, **kwargs):
                captured["gopay_cfg"] = dict(gopay_cfg)
                captured["charger_kwargs"] = dict(kwargs)
                self.closed = False
                self.midtrans_tokenization = gopay_cfg.get("tokenization", "true")

            def prepare_until_linking(self, stripe_pk="", billing=None, checkout_session_id="", checkout_url=""):
                captured["prepare"] = {
                    "stripe_pk": stripe_pk,
                    "billing": dict(billing or {}),
                    "checkout_session_id": checkout_session_id,
                    "checkout_url": checkout_url,
                }
                return {
                    "state": "prepared",
                    "snap_token": "snap_prepared",
                    "cs_id": checkout_session_id or "cs_prepared",
                    "checkout_url": checkout_url or "https://checkout.stripe.com/c/pay/cs_prepared",
                }

            def start_prepared_linking_until_otp(self, state, otp_channel="", gopay_phone=""):
                captured["start_prepared"] = {
                    "state": dict(state),
                    "otp_channel": otp_channel,
                    "gopay_phone": gopay_phone,
                }
                return {
                    **state,
                    "state": "otp",
                    "issued_after_unix": 456,
                    "otp_required": True,
                }

            def close(self):
                self.closed = True
                captured["charger_closed"] = True

        def fake_build_chatgpt_session(auth_cfg, proxy=None):
            captured["auth_cfg"] = dict(auth_cfg)
            captured["build_proxy"] = proxy
            return FakeChatGPTSession()

        svc = PaymentService({"fresh_checkout": {"auth": {}}, "gopay": {"country_code": "62", "phone_number": "", "pin": TEST_PIN}})

        with patch.object(payment_server, "_build_chatgpt_session", fake_build_chatgpt_session), \
                patch.object(payment_server, "resolve_checkout_proxy", return_value="socks5://checkout"), \
                patch.object(payment_server, "resolve_payment_proxy", return_value="socks5://payment"), \
                patch.object(payment_server, "GoPayCharger", FakeGoPayCharger):
            prepared = svc.PrepareGoPay(
                payment_pb2.PrepareGoPayRequest(
                    access_token="chatgpt-token",
                    tokenization="true",
                    checkout_session_id="cs_probe",
                    checkout_url="https://checkout.stripe.com/c/pay/cs_probe",
                    gopay_phone=TEST_ACCOUNT_PHONE,
                ),
                None,
            )
            started = svc.StartPreparedGoPay(
                payment_pb2.StartPreparedGoPayRequest(
                    flow_id=prepared.flow_id,
                    gopay_phone="81234567890",
                    otp_channel="sms",
                ),
                None,
            )

        self.assertTrue(prepared.success)
        self.assertEqual(prepared.snap_token, "snap_prepared")
        self.assertEqual(prepared.checkout_session_id, "cs_probe")
        self.assertTrue(started.success)
        self.assertTrue(started.otp_required)
        self.assertEqual(started.flow_id, prepared.flow_id)
        self.assertEqual(started.issued_after_unix, 456)
        self.assertEqual(captured["auth_cfg"]["access_token"], "chatgpt-token")
        self.assertEqual(captured["gopay_cfg"]["phone_number"], TEST_ACCOUNT_PHONE)
        self.assertEqual(captured["gopay_cfg"]["tokenization"], "true")
        self.assertEqual(captured["prepare"]["checkout_session_id"], "cs_probe")
        self.assertEqual(captured["start_prepared"]["otp_channel"], "sms")
        self.assertEqual(captured["start_prepared"]["gopay_phone"], "81234567890")
        self.assertFalse(captured.get("charger_closed", False))
        self.assertIsNotNone(svc._flows.get(prepared.flow_id))
        svc.close()
        self.assertTrue(captured["charger_closed"])

    def test_resend_gopay_otp_keeps_flow_open(self):
        class FakeResendCharger(FakeCharger):
            def resend_linking_otp(self, state):
                self.resend_state = dict(state)
                return {
                    **state,
                    "issued_after_unix": 789,
                    "otp_resend_count": int(state.get("otp_resend_count") or 0) + 1,
                }

        svc = PaymentService({"fresh_checkout": {"auth": {}}, "gopay": {"country_code": "62", "phone_number": "81234567890", "pin": TEST_PIN}})
        charger = FakeResendCharger()
        flow_id = svc._flows.put(charger, {"snap_token": "snap", "reference_id": "ref"})

        resp = svc.ResendGoPayOTP(payment_pb2.ResendGoPayOTPRequest(flow_id=flow_id), None)

        self.assertTrue(resp.success)
        self.assertEqual(resp.flow_id, flow_id)
        self.assertEqual(resp.issued_after_unix, 789)
        self.assertEqual(charger.resend_state["reference_id"], "ref")
        self.assertFalse(charger.closed)
        self.assertIsNotNone(svc._flows.get(flow_id))

    def test_complete_gopay_waits_for_manual_confirmation_then_confirms(self):
        class FakeSplitCharger(FakeCharger):
            def complete_after_otp_until_manual_confirmation(self, state, otp):
                self.otp = otp
                return {
                    **state,
                    "state": "awaiting_manual_confirmation",
                    "charge_ref": "A123",
                    "snap_token": "snap",
                    "deeplink_url": "gopay://pay?reference=A123",
                    "finish_redirect_url": "https://return.example/pending",
                    "finish_200_redirect_url": "https://return.example/success",
                }

            def complete_after_manual_confirmation(self, state):
                self.confirm_state = dict(state)
                return {
                    **state,
                    "state": "succeeded",
                    "charge_ref": state["charge_ref"],
                    "snap_token": state["snap_token"],
                }

        svc = PaymentService({"fresh_checkout": {"auth": {}}, "gopay": {"country_code": "62", "phone_number": "81234567890", "pin": TEST_PIN}})
        charger = FakeSplitCharger()
        charger.midtrans_tokenization = "false"
        flow_id = svc._flows.put(charger, {"snap_token": "snap", "reference_id": "ref"})

        waiting = svc.CompleteGoPay(payment_pb2.CompleteGoPayRequest(flow_id=flow_id, otp="123456"), None)

        self.assertTrue(waiting.success)
        self.assertTrue(waiting.awaiting_manual_confirmation)
        self.assertEqual(waiting.charge_ref, "A123")
        self.assertEqual(waiting.deeplink_url, "gopay://pay?reference=A123")
        self.assertFalse(charger.closed)
        self.assertIsNotNone(svc._flows.get(flow_id))

        done = svc.ConfirmGoPayPayment(payment_pb2.ConfirmGoPayPaymentRequest(flow_id=flow_id), None)

        self.assertTrue(done.success)
        self.assertEqual(done.charge_ref, "A123")
        self.assertTrue(charger.closed)
        self.assertIsNone(svc._flows.get(flow_id))

    def test_complete_gopay_account_tokenization_false_waits_for_external_confirmation(self):
        calls = []

        class FakeSplitCharger(FakeCharger):
            def complete_after_otp_until_manual_confirmation(self, state, otp):
                calls.append(("otp", otp))
                return {
                    **state,
                    "state": "awaiting_manual_confirmation",
                    "charge_ref": "A123",
                    "snap_token": "snap",
                    "deeplink_url": "gopay://pay?reference=A220260514182119XINUsw6rOZID",
                    "finish_redirect_url": "https://return.example/pending",
                }

        svc = PaymentService({"fresh_checkout": {"auth": {}}, "gopay": {"country_code": "62", "phone_number": "81234567890", "pin": TEST_PIN}})
        charger = FakeSplitCharger()
        charger.midtrans_tokenization = "false"
        flow_id = svc._flows.put(charger, {"snap_token": "snap", "reference_id": "ref"}, use_account_token=True)

        done = svc.CompleteGoPay(payment_pb2.CompleteGoPayRequest(flow_id=flow_id, otp="123456"), None)

        self.assertTrue(done.success)
        self.assertTrue(done.awaiting_manual_confirmation)
        self.assertEqual(done.charge_ref, "A123")
        self.assertEqual(done.deeplink_url, "gopay://pay?reference=A220260514182119XINUsw6rOZID")
        self.assertEqual(calls, [("otp", "123456")])
        self.assertFalse(charger.closed)
        self.assertIsNotNone(svc._flows.get(flow_id))

    def test_complete_gopay_without_otp_continues_when_otp_not_required(self):
        class FakeNoOTPCharger(FakeCharger):
            def complete_after_otp(self, state, otp):
                raise AssertionError("otp path should not run")

            def complete_after_manual_confirmation(self, state):
                self.confirm_state = dict(state)
                return {
                    **state,
                    "state": "succeeded",
                    "charge_ref": state["charge_ref"],
                    "snap_token": state["snap_token"],
                }

        svc = PaymentService({"fresh_checkout": {"auth": {}}, "gopay": {"country_code": "62", "phone_number": "81234567890", "pin": TEST_PIN}})
        charger = FakeNoOTPCharger()
        charger.midtrans_tokenization = "true"
        flow_id = svc._flows.put(charger, {
            "snap_token": "snap",
            "charge_ref": "A123",
            "otp_required": False,
        })

        done = svc.CompleteGoPay(payment_pb2.CompleteGoPayRequest(flow_id=flow_id), None)

        self.assertTrue(done.success)
        self.assertEqual(done.charge_ref, "A123")
        self.assertEqual(charger.confirm_state["charge_ref"], "A123")
        self.assertTrue(charger.closed)
        self.assertIsNone(svc._flows.get(flow_id))

    def test_complete_gopay_tokenization_true_keeps_single_step_flow(self):
        class FakeLegacyCharger(FakeCharger):
            def complete_after_otp(self, state, otp):
                self.otp = otp
                return {
                    **state,
                    "state": "succeeded",
                    "charge_ref": "A123",
                    "snap_token": "snap",
                }

            def complete_after_otp_until_manual_confirmation(self, state, otp):
                raise AssertionError("manual confirmation split should not run for tokenization=true")

        svc = PaymentService({"fresh_checkout": {"auth": {}}, "gopay": {"country_code": "62", "phone_number": "81234567890", "pin": TEST_PIN}})
        charger = FakeLegacyCharger()
        charger.midtrans_tokenization = "true"
        flow_id = svc._flows.put(charger, {"snap_token": "snap", "reference_id": "ref"})

        done = svc.CompleteGoPay(payment_pb2.CompleteGoPayRequest(flow_id=flow_id, otp="123456"), None)

        self.assertTrue(done.success)
        self.assertFalse(done.awaiting_manual_confirmation)
        self.assertEqual(done.charge_ref, "A123")
        self.assertEqual(charger.otp, "123456")
        self.assertTrue(charger.closed)
        self.assertIsNone(svc._flows.get(flow_id))


if __name__ == "__main__":
    unittest.main()
