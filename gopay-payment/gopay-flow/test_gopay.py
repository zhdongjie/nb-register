import base64
import json
import unittest
from unittest.mock import patch

from gopay import (
    GoPayCharger,
    GoPayError,
    GoPayOTPRejected,
    _extract_passive_captcha_config,
    _extract_midtrans_charge_reference,
    _midtrans_charge_denial_message,
    _request_with_retries,
    _resolve_expected_amount,
    _session_cookie_parts,
    _stripe_confirm_error_detail,
    _detect_plus_active_from_session_payload,
    probe_plus_active_session_token,
    probe_tier_access_token,
    probe_plus_trial_checkout,
)


class FakeResponse:
    def __init__(self, status_code, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise GoPayError(f"http {self.status_code}: {self.text}")


class FakeExt:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.proxies = {}

    def get(self, *args, **kwargs):
        return self.response

    def post(self, *args, **kwargs):
        return self.response

    def close(self):
        pass


class FakeChatGPTSession:
    def __init__(self):
        self.headers = {"User-Agent": "test-agent"}
        self.proxies = {}
        self.closed = False
        self.posts = []

    def post(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        return FakeResponse(200, payload={
            "url": "https://checkout.stripe.com/c/pay/cs_test_probe#fidkdWxOYHwnPyd1",
        })

    def close(self):
        self.closed = True


class CapturingPostSession:
    def __init__(self, response):
        self.response = response
        self.headers = {"User-Agent": "test-agent"}
        self.proxies = {}
        self.posts = []

    def post(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        return self.response

    def close(self):
        pass


class FlakySession:
    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("TLS connect error: transient")
        return FakeResponse(200, payload={"ok": True})


class FlakyGoPayExt:
    def __init__(self):
        self.calls = 0
        self.headers = {}
        self.proxies = {}

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("curl: (35) TLS connect error: invalid library")
        return FakeResponse(200, payload={
            "success": True,
            "data": {
                "challenge": {
                    "action": {
                        "value": {
                            "challenge_id": "challenge-1",
                            "client_id": "client-1",
                        },
                    },
                },
            },
        })


class GoPayValidateOtpTests(unittest.TestCase):
    def charger_for(self, response):
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.ext = FakeExt(response)
        charger.browser_locale = "zh-CN"
        return charger

    def test_validate_otp_400_is_retryable_otp_error(self):
        charger = self.charger_for(FakeResponse(400, '{"success":false,"error":"invalid otp"}'))

        with self.assertRaises(GoPayOTPRejected) as raised:
            charger._gopay_validate_otp("ref", "111111")

        self.assertIn("validate-otp 400", str(raised.exception))
        self.assertIn("invalid otp", str(raised.exception))

    def test_validate_otp_unsuccessful_200_is_retryable_otp_error(self):
        charger = self.charger_for(FakeResponse(200, payload={"success": False, "error": "bad otp"}))

        with self.assertRaises(GoPayOTPRejected):
            charger._gopay_validate_otp("ref", "111111")

    def test_validate_otp_retries_tls_transport_error(self):
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.ext = FlakyGoPayExt()
        charger.browser_locale = "zh-CN"
        charger.log = lambda _msg: None

        challenge_id, client_id = charger._gopay_validate_otp("ref", "111111")

        self.assertEqual(challenge_id, "challenge-1")
        self.assertEqual(client_id, "client-1")
        self.assertEqual(charger.ext.calls, 2)

    def test_validate_reference_400_includes_response_body(self):
        body = '{"success":false,"errors":[{"code":"GoPay-5001","message":"retry later"}]}'
        charger = self.charger_for(FakeResponse(400, body))

        with self.assertRaises(GoPayError) as raised:
            charger._gopay_validate_reference("ref")

        message = str(raised.exception)
        self.assertIn("validate-reference 400", message)
        self.assertIn("GoPay-5001", message)


class RetryTransportTests(unittest.TestCase):
    def test_retries_retryable_transport_error(self):
        session = FlakySession()

        resp = _request_with_retries(
            session,
            "post",
            "https://api.stripe.com/v1/payment_methods",
            log=lambda _msg: None,
            delay_seconds=0,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(session.calls, 2)


class MidtransChargeReferenceTests(unittest.TestCase):
    def test_extracts_reference_from_legacy_verification_link(self):
        ref = _extract_midtrans_charge_reference({
            "gopay_verification_link_url": "https://example.test/pay?reference=A123BC",
        })

        self.assertEqual(ref, "A123BC")

    def test_extracts_reference_from_nested_action_url(self):
        ref = _extract_midtrans_charge_reference({
            "actions": [
                {"name": "get-status", "url": "https://example.test/status"},
                {"name": "verify", "url": "gojek://pay#reference=A987ZZ"},
            ],
        })

        self.assertEqual(ref, "A987ZZ")

    def test_extracts_reference_from_explicit_reference_field(self):
        ref = _extract_midtrans_charge_reference({"payment_reference": "A555AA"})

        self.assertEqual(ref, "A555AA")

    def test_denial_message_is_explicit(self):
        message = _midtrans_charge_denial_message({
            "status_code": "202",
            "status_message": "Your transaction is denied.",
            "transaction_status": "deny",
            "fraud_status": "deny",
            "order_id": "setatt_test",
            "gross_amount": "1",
            "currency": "IDR",
        })

        self.assertIn("midtrans charge denied", message)
        self.assertIn("transaction_status=deny", message)
        self.assertIn("fraud_status=deny", message)
        self.assertIn("gross_amount=1", message)

    def test_create_charge_uses_configured_tokenization_and_redacts_response_logs(self):
        payload = {
            "payment_reference": "A555AA",
            "actions": [
                {
                    "name": "deeplink",
                    "url": (
                        "gopay%3A%2F%2Fpay%3Freference%3DA555AA%26callback_url%3D"
                        "https%253A%252F%252Fpm-redirects.example%252Freturn%253Forder_id%253Dsetatt_test"
                    ),
                },
                {
                    "name": "finish_redirect_url",
                    "url": "https%3A%2F%2Fpm-redirects.example%2Freturn%3Forder_id%3Dsetatt_test",
                },
            ],
        }
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.ext = CapturingPostSession(FakeResponse(200, payload=payload))
        charger.payment_proxy = None
        charger.midtrans_tokenization = "false"
        logs = []
        charger.log = logs.append

        charge_ref = GoPayCharger._midtrans_create_charge(charger, "snap_test")

        self.assertEqual(charge_ref, "A555AA")
        body = charger.ext.posts[0][1]["json"]
        self.assertEqual(body["payment_type"], "gopay")
        self.assertEqual(body["tokenization"], "false")
        joined_logs = "\n".join(logs)
        self.assertIn("midtrans charge response", joined_logs)
        self.assertIn("midtrans deeplink_url=present", joined_logs)
        self.assertIn("midtrans finish_redirect_url=present", joined_logs)
        self.assertNotIn("gopay://pay", joined_logs)
        self.assertNotIn("callback_url", joined_logs)
        self.assertNotIn("order_id=setatt_test", joined_logs)


class ManualConfirmationRoutingTests(unittest.TestCase):
    def base_charger(self, tokenization):
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.midtrans_tokenization = tokenization
        charger.log = lambda _msg: None
        return charger

    def test_tokenization_false_uses_midtrans_status_without_gopay_confirm(self):
        charger = self.base_charger("false")
        calls = []
        charger._midtrans_poll_status = lambda snap: calls.append(("poll", snap)) or {
            "transaction_status": "settlement",
            "status_code": "200",
            "finish_redirect_url": "https://pm-redirects.example/return",
        }
        charger._follow_midtrans_finish_redirect = (
            lambda state, status: calls.append(("finish", status["transaction_status"]))
        )
        charger._chatgpt_verify = lambda cs: calls.append(("verify", cs)) or {
            "state": "succeeded",
            "cs_id": cs,
        }
        charger._gopay_payment_validate = lambda _ref: self.fail("payment/validate should be skipped")
        charger._gopay_payment_confirm = lambda _ref: self.fail("payment/confirm should be skipped")
        charger._tokenize_pin = lambda *_args, **_kwargs: self.fail("payment PIN tokenization should be skipped")
        charger._gopay_payment_process = lambda *_args: self.fail("payment/process should be skipped")

        result = GoPayCharger.complete_after_manual_confirmation(charger, {
            "snap_token": "snap",
            "charge_ref": "A123",
            "cs_id": "cs_test",
            "deeplink_url": "gopay://pay",
        })

        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(calls, [
            ("poll", "snap"),
            ("finish", "settlement"),
            ("verify", "cs_test"),
        ])

    def test_tokenization_true_keeps_gopay_confirm_process_path(self):
        charger = self.base_charger("true")
        calls = []
        charger._gopay_payment_validate = lambda ref: calls.append(("validate", ref))
        charger._gopay_payment_confirm = lambda ref: calls.append(("confirm", ref)) or ("challenge", "client")
        charger._tokenize_pin = (
            lambda challenge, client, *, purpose: calls.append(("pin", challenge, client, purpose)) or "pin-token"
        )
        charger._gopay_payment_process = lambda ref, token: calls.append(("process", ref, token))
        charger._midtrans_poll_status = lambda snap: calls.append(("poll", snap)) or {
            "transaction_status": "capture",
            "status_code": "200",
        }
        charger._follow_midtrans_finish_redirect = (
            lambda *_args: self.fail("finish redirect should not run for tokenization=true")
        )
        charger._chatgpt_verify = lambda cs: calls.append(("verify", cs)) or {
            "state": "succeeded",
            "cs_id": cs,
        }

        result = GoPayCharger.complete_after_manual_confirmation(charger, {
            "snap_token": "snap",
            "charge_ref": "A123",
            "cs_id": "cs_test",
        })

        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(calls, [
            ("validate", "A123"),
            ("confirm", "A123"),
            ("pin", "challenge", "client", "payment"),
            ("process", "A123", "pin-token"),
            ("poll", "snap"),
            ("verify", "cs_test"),
        ])


class StripeExpectedAmountTests(unittest.TestCase):
    def test_create_checkout_uses_hosted_plus_promo_shape_and_checkout_proxy(self):
        chatgpt = FakeChatGPTSession()
        charger = GoPayCharger(
            chatgpt,
            {"country_code": "0", "phone_number": "0", "pin": "0"},
            otp_provider=lambda: "",
            checkout_proxy="socks5://checkout",
            payment_proxy="socks5://payment",
            log=lambda _msg: None,
        )

        try:
            cs_id = charger._chatgpt_create_checkout()
        finally:
            charger.close()

        self.assertEqual(cs_id, "cs_test_probe")
        self.assertEqual(charger.checkout_url, "https://checkout.stripe.com/c/pay/cs_test_probe#fidkdWxOYHwnPyd1")
        self.assertEqual(chatgpt.proxies, {"http": "socks5://checkout", "https": "socks5://checkout"})
        body = chatgpt.posts[0][1]["json"]
        self.assertEqual(body["plan_name"], "chatgptplusplan")
        self.assertEqual(body["billing_details"], {"country": "ID", "currency": "IDR"})
        self.assertEqual(body["promo_campaign"]["promo_campaign_id"], "plus-1-month-free")
        self.assertEqual(body["checkout_ui_mode"], "hosted")
        self.assertEqual(body["cancel_url"], "https://chatgpt.com/#pricing")

    def test_uses_zero_amount_from_checkout_session(self):
        amount, source = _resolve_expected_amount(
            {"currency": "idr", "checkout_session": {"amount_total": 0}},
            {},
        )

        self.assertEqual(amount, "0")
        self.assertEqual(source, "checkout_session.amount_total")

    def test_prefers_total_summary_due_over_invoice_amount_due(self):
        amount, source = _resolve_expected_amount(
            {
                "currency": "idr",
                "total_summary": {"due": 0, "total": 34900000},
                "invoice": {"amount_due": 34900000},
            },
            {},
        )

        self.assertEqual(amount, "0")
        self.assertEqual(source, "total_summary.due")

    def test_refuses_nonzero_amount_by_default(self):
        with self.assertRaises(GoPayError) as raised:
            _resolve_expected_amount(
                {"currency": "idr", "latest_invoice": {"amount_due": 319000}},
                {},
            )

        self.assertIn("not free-trial 0", str(raised.exception))

    def test_allows_nonzero_amount_when_explicitly_configured(self):
        amount, source = _resolve_expected_amount(
            {"currency": "idr", "latest_invoice": {"amount_due": 319000}},
            {"allow_nonzero_expected_amount": True},
        )

        self.assertEqual(amount, "319000")
        self.assertEqual(source, "latest_invoice.amount_due")

    def test_runtime_expected_amount_override(self):
        amount, source = _resolve_expected_amount(
            {"currency": "idr", "latest_invoice": {"amount_due": 319000}},
            {"expected_amount": "0"},
        )

        self.assertEqual(amount, "0")
        self.assertEqual(source, "runtime.expected_amount")

    def test_checkout_amount_mismatch_error_mentions_sent_amount(self):
        detail = _stripe_confirm_error_detail(
            '{"error":{"code":"checkout_amount_mismatch","message":"amount changed"}}',
            expected_amount="0",
            expected_amount_source="fallback_zero_unknown",
        )

        self.assertIn("sent expected_amount=0", detail)
        self.assertIn("another checkout", detail)


class PassiveCaptchaTests(unittest.TestCase):
    def test_extracts_passive_captcha_from_init_payload(self):
        cfg = _extract_passive_captcha_config({
            "passive_captcha": {
                "site_key": "site-key-1",
                "rqdata": "rq-data-1",
            },
        })

        self.assertEqual(cfg["site_key"], "site-key-1")
        self.assertEqual(cfg["rqdata"], "rq-data-1")
        self.assertTrue(cfg["is_invisible"])
        self.assertIn("HCaptchaInvisible.html", cfg["website_url"])

    def test_passive_captcha_uses_browser_solver(self):
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.browser_challenge_cfg = {"enabled": True, "use_for_passive_captcha": True}
        charger._midtrans_merchant_id = "merchant-1"
        charger.browser_locale = "en-US"
        charger.log = lambda _msg: None
        init_data = {"passive_captcha": {"site_key": "site-key-1", "rqdata": "rq-data-1"}}

        with patch("gopay._solve_passive_hcaptcha_in_browser", return_value=("token-1", "ekey-1")) as solve:
            token, ekey = GoPayCharger._solve_passive_confirm_captcha(charger, init_data)

        self.assertEqual((token, ekey), ("token-1", "ekey-1"))
        self.assertEqual(solve.call_args.kwargs["browser_cfg"], charger.browser_challenge_cfg)
        self.assertEqual(solve.call_args.kwargs["merchant_id"], "merchant-1")

    def test_stripe_confirm_sends_passive_captcha_token(self):
        ext = CapturingPostSession(FakeResponse(200, payload={"payment_status": "open"}))
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.ext = ext
        charger.runtime = {}
        charger.log = lambda _msg: None
        charger._stripe_init = lambda _cs, _pk: {
            "init_checksum": "chk",
            "currency": "idr",
            "payment_method_types": ["gopay"],
            "checkout_session": {"amount_total": 0},
        }
        charger._solve_passive_confirm_captcha = lambda _init: ("captcha-token", "captcha-ekey")

        GoPayCharger._stripe_confirm(
            charger,
            "cs_test",
            "pm_test",
            "pk_test",
            force_passive_captcha=True,
        )

        body = ext.posts[0][1]["data"]
        self.assertEqual(body["passive_captcha_token"], "captcha-token")
        self.assertEqual(body["passive_captcha_ekey"], "captcha-ekey")

    def test_start_until_otp_retries_confirm_with_passive_captcha_on_approve_blocked(self):
        charger = GoPayCharger.__new__(GoPayCharger)
        charger.log = lambda _msg: None
        charger.pre_solve_passive_captcha = False
        charger._chatgpt_create_checkout = lambda: "cs_test"

        pm_calls = []
        charger._stripe_create_pm = lambda _cs, _pk, _billing: (
            pm_calls.append(True) or f"pm_{len(pm_calls)}"
        )

        confirm_calls = []

        def fake_confirm(_cs, pm_id, _pk, *, force_passive_captcha=False, **_kwargs):
            confirm_calls.append((pm_id, force_passive_captcha))
            return {}

        charger._stripe_confirm = fake_confirm
        charger._extract_redirect_to_url = lambda _data: ""

        approve_calls = []

        def fake_approve(_cs):
            approve_calls.append(True)
            if len(approve_calls) == 1:
                raise GoPayError("chatgpt approve: result='blocked'")

        charger._chatgpt_approve = fake_approve
        charger._follow_redirect_to_midtrans = lambda _cs, _pk: "snap"
        charger.start_linking_until_otp = lambda snap, cs, pk: {
            "snap_token": snap,
            "cs_id": cs,
            "stripe_pk": pk,
        }

        result = GoPayCharger.start_until_otp(charger, "pk_test", billing={})

        self.assertEqual(confirm_calls, [("pm_1", False), ("pm_2", True)])
        self.assertEqual(len(approve_calls), 2)
        self.assertEqual(result["snap_token"], "snap")


class PlusTrialProbeTests(unittest.TestCase):
    def jwt_for_payload(self, payload):
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"header.{encoded}.signature"

    def test_session_payload_detects_account_plan_type(self):
        result = _detect_plus_active_from_session_payload({
            "user": {"id": "user-1"},
            "account": {"planType": "plus"},
        })

        self.assertTrue(result["checked"])
        self.assertTrue(result["plus_active"])
        self.assertEqual(result["tier"], "plus")
        self.assertEqual(result["source"], "auth_session:account.planType")

    def test_session_payload_detects_access_token_plan_type(self):
        access_token = self.jwt_for_payload({
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus",
            },
        })

        result = _detect_plus_active_from_session_payload({
            "user": {"id": "user-1"},
            "accessToken": access_token,
        })

        self.assertTrue(result["checked"])
        self.assertTrue(result["plus_active"])
        self.assertEqual(result["tier"], "plus")
        self.assertEqual(result["source"], "auth_session:accessToken.auth.chatgpt_plan_type")

    def test_session_payload_prefers_access_token_plan_over_account_plan(self):
        access_token = self.jwt_for_payload({
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus",
            },
        })

        result = _detect_plus_active_from_session_payload({
            "user": {"id": "user-1"},
            "account": {"planType": "free"},
            "accessToken": access_token,
        })

        self.assertTrue(result["checked"])
        self.assertTrue(result["plus_active"])
        self.assertEqual(result["tier"], "plus")
        self.assertEqual(result["source"], "auth_session:accessToken.auth.chatgpt_plan_type")

    def test_session_payload_detects_plus_group(self):
        result = _detect_plus_active_from_session_payload({
            "user": {"groups": ["chatgpt_plus_user"]},
            "accessToken": "token",
        })

        self.assertTrue(result["checked"])
        self.assertTrue(result["plus_active"])
        self.assertEqual(result["plan_type"], "plus")

    def test_session_cookie_parts_keep_browser_chunks(self):
        parts = _session_cookie_parts(
            "oai-did=device; __Secure-next-auth.session-token.1=tail; "
            "__Secure-next-auth.session-token.0=head; other=value"
        )

        self.assertEqual(parts, [
            "__Secure-next-auth.session-token.0=head",
            "__Secure-next-auth.session-token.1=tail",
        ])

    def test_session_cookie_parts_chunk_long_raw_token(self):
        token = "a" * (4096 - 163) + "bc"
        parts = _session_cookie_parts(token)

        self.assertEqual(parts, [
            "__Secure-next-auth.session-token.0=" + ("a" * (4096 - 163)),
            "__Secure-next-auth.session-token.1=bc",
        ])

    def test_session_cookie_parts_accept_auth_session_json(self):
        parts = _session_cookie_parts(json.dumps({"sessionToken": "session-token"}))

        self.assertEqual(parts, ["__Secure-next-auth.session-token=session-token"])

    def test_session_token_probe_uses_auth_session(self):
        session_resp = FakeResponse(
            200,
            payload={
                "user": {"id": "user-1"},
                "account": {"plan_type": "plus"},
            },
        )

        with patch("gopay._new_session", return_value=FakeExt(session_resp)):
            result = probe_plus_active_session_token("session-token", log=lambda _msg: None)

        self.assertTrue(result["checked"])
        self.assertTrue(result["plus_active"])
        self.assertEqual(result["plan_type"], "plus")

    def test_session_token_probe_sends_chunked_cookie_names(self):
        session_resp = FakeResponse(
            200,
            payload={
                "user": {"id": "user-1"},
                "account": {"plan_type": "plus"},
            },
        )
        fake = FakeExt(session_resp)

        with patch("gopay._new_session", return_value=fake):
            probe_plus_active_session_token("a" * (4096 - 163) + "bc", log=lambda _msg: None)

        self.assertIn("__Secure-next-auth.session-token.0=", fake.headers["Cookie"])
        self.assertIn("__Secure-next-auth.session-token.1=bc", fake.headers["Cookie"])

    def test_session_token_probe_prefers_wham_usage_plan(self):
        access_token = self.jwt_for_payload({
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus",
                "chatgpt_account_id": "account-1",
            },
        })
        session_resp = FakeResponse(
            200,
            payload={
                "user": {"id": "user-1"},
                "account": {"plan_type": "free"},
                "accessToken": access_token,
            },
        )
        wham_resp = FakeResponse(200, payload={"plan_type": "pro"})
        wham = FakeExt(wham_resp)

        with patch("gopay._new_session", side_effect=[FakeExt(session_resp), wham]):
            result = probe_plus_active_session_token("session-token", log=lambda _msg: None)

        self.assertTrue(result["checked"])
        self.assertTrue(result["plus_active"])
        self.assertEqual(result["tier"], "pro")
        self.assertEqual(result["source"], "wham_usage.plan_type")
        self.assertEqual(wham.headers["ChatGPT-Account-Id"], "account-1")

    def test_session_token_probe_falls_back_to_access_token_claim(self):
        access_token = self.jwt_for_payload({
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus",
            },
        })
        session_resp = FakeResponse(
            200,
            payload={
                "user": {"id": "user-1"},
                "account": {"plan_type": "free"},
                "accessToken": access_token,
            },
        )
        wham_resp = FakeResponse(403, text="forbidden")

        with patch("gopay._new_session", side_effect=[FakeExt(session_resp), FakeExt(wham_resp)]):
            result = probe_plus_active_session_token("session-token", log=lambda _msg: None)

        self.assertTrue(result["checked"])
        self.assertTrue(result["plus_active"])
        self.assertEqual(result["tier"], "plus")
        self.assertEqual(result["source"], "accessToken.auth.chatgpt_plan_type")

    def test_access_token_probe_reads_wham_usage(self):
        wham = FakeExt(FakeResponse(200, payload={"plan_type": "team"}))

        with patch("gopay._new_session", return_value=wham):
            result = probe_tier_access_token("access-token", account_id="account-1", log=lambda _msg: None)

        self.assertTrue(result["checked"])
        self.assertTrue(result["plus_active"])
        self.assertEqual(result["tier"], "team")
        self.assertEqual(result["source"], "wham_usage.plan_type")
        self.assertEqual(wham.headers["Authorization"], "Bearer access-token")
        self.assertEqual(wham.headers["ChatGPT-Account-Id"], "account-1")

    def test_session_token_probe_marks_authenticated_without_paid_marker_free(self):
        session_resp = FakeResponse(
            200,
            payload={
                "user": {"id": "user-1"},
                "accessToken": "token",
            },
        )

        with patch("gopay._new_session", return_value=FakeExt(session_resp)):
            result = probe_plus_active_session_token("session-token", log=lambda _msg: None)

        self.assertTrue(result["checked"])
        self.assertFalse(result["plus_active"])
        self.assertEqual(result["tier"], "free")

    def test_probe_marks_zero_amount_eligible(self):
        stripe_init = FakeResponse(
            200,
            payload={
                "currency": "idr",
                "init_checksum": "chk",
                "payment_method_types": ["gopay"],
                "checkout_session": {"amount_total": 0},
            },
        )

        with patch("gopay._new_session", return_value=FakeExt(stripe_init)):
            result = probe_plus_trial_checkout(FakeChatGPTSession(), log=lambda _msg: None)

        self.assertTrue(result["checked"])
        self.assertTrue(result["plus_trial_eligible"])
        self.assertEqual(result["amount"], 0)
        self.assertEqual(result["source"], "checkout_session.amount_total")

    def test_probe_marks_nonzero_amount_ineligible(self):
        stripe_init = FakeResponse(
            200,
            payload={
                "currency": "idr",
                "init_checksum": "chk",
                "payment_method_types": ["gopay"],
                "latest_invoice": {"amount_due": 319000},
            },
        )

        with patch("gopay._new_session", return_value=FakeExt(stripe_init)):
            result = probe_plus_trial_checkout(FakeChatGPTSession(), log=lambda _msg: None)

        self.assertTrue(result["checked"])
        self.assertFalse(result["plus_trial_eligible"])
        self.assertEqual(result["amount"], 319000)


if __name__ == "__main__":
    unittest.main()
