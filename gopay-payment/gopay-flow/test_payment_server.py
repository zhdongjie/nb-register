import unittest
from unittest.mock import patch

import payment_pb2
import payment_server
from payment_server import FlowStore, PaymentService


TEST_CYCLE_PHONE = "test-cycle-phone"
TEST_PIN = "000000"


class FakeCharger:
    def __init__(self):
        self.closed = False
        self.midtrans_tokenization = "true"

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
    def test_cycle_token_does_not_replace_chatgpt_access_token(self):
        captured = {}

        class FakeChatGPTSession:
            headers = {}

            def close(self):
                pass

        class FakeGoPayCharger:
            def __init__(self, chatgpt_session, gopay_cfg, **kwargs):
                captured["gopay_cfg"] = dict(gopay_cfg)
                captured["charger_kwargs"] = dict(kwargs)

            def start_until_otp(self, stripe_pk="", billing=None):
                return {"snap_token": "snap", "issued_after_unix": 123}

        def fake_build_chatgpt_session(auth_cfg, proxy=None):
            captured["auth_cfg"] = dict(auth_cfg)
            captured["build_proxy"] = proxy
            return FakeChatGPTSession()

        svc = PaymentService({"fresh_checkout": {"auth": {}}, "gopay": {"country_code": "62", "phone_number": "", "pin": TEST_PIN}})

        with patch.object(svc, "_ready_cycle_access_token", return_value=("cycle-gopay-token", TEST_CYCLE_PHONE)), \
                patch.object(payment_server, "_build_chatgpt_session", fake_build_chatgpt_session), \
                patch.object(payment_server, "resolve_checkout_proxy", return_value="socks5://checkout"), \
                patch.object(payment_server, "resolve_payment_proxy", return_value="socks5://payment"), \
                patch.object(payment_server, "GoPayCharger", FakeGoPayCharger):
            resp = svc.StartGoPay(
                payment_pb2.StartGoPayRequest(access_token="test-token", use_cycle_token=True),
                None,
            )

        self.assertTrue(resp.success)
        self.assertEqual(captured["auth_cfg"]["access_token"], "test-token")
        self.assertEqual(captured["gopay_cfg"]["phone_number"], TEST_CYCLE_PHONE)
        self.assertEqual(captured["build_proxy"], "socks5://checkout")
        self.assertEqual(captured["charger_kwargs"]["checkout_proxy"], "socks5://checkout")
        self.assertEqual(captured["charger_kwargs"]["payment_proxy"], "socks5://payment")

    def test_start_gopay_passes_tokenization_override(self):
        captured = {}

        class FakeChatGPTSession:
            headers = {}

            def close(self):
                pass

        class FakeGoPayCharger:
            def __init__(self, chatgpt_session, gopay_cfg, **kwargs):
                captured["gopay_cfg"] = dict(gopay_cfg)

            def start_until_otp(self, stripe_pk="", billing=None):
                return {"snap_token": "snap", "issued_after_unix": 123}

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
