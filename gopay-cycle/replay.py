"""Direct QR payment executor for GoPay Cycle."""

from __future__ import annotations

import json
import time
import uuid
import zlib
from dataclasses import dataclass
from typing import Any


GOPAY_CUSTOMER = "https://customer.gopayapi.com"
DEFAULT_CURRENCY = "IDR"
DEFAULT_SERVICE_ID = "1001"


@dataclass
class QrPaymentOptions:
    qr_code: str = ""
    order_json: str = ""
    pin: str = ""
    amount_value: int = 0
    amount_currency: str = DEFAULT_CURRENCY
    body_limit: int = 1200


def run_qr_payment(client, options: QrPaymentOptions) -> dict[str, Any]:
    """Execute the app-side QR payment flow using the caller's DB-backed token."""
    steps: list[dict[str, Any]] = []
    try:
        if not options.pin:
            raise ValueError("pin required")

        if options.order_json:
            order = _load_order(options.order_json)
        else:
            if not options.qr_code:
                raise ValueError("qr_code or order_json required")
            qris_body = build_qris_payment_body(
                options.qr_code,
                amount_value=options.amount_value,
                amount_currency=options.amount_currency or DEFAULT_CURRENCY,
            )
            qris = _call(
                steps,
                "qris_payment",
                lambda: client.post(
                    f"{GOPAY_CUSTOMER}/v1/qris/payments",
                    body=qris_body,
                    extra_headers={"Idempotency-Key": str(uuid.uuid1())},
                ),
                options,
            )
            _require_status("qris_payment", qris, {200})
            order = qris.get("data") or {}

        payment_id = str(order.get("payment_id") or "").strip()
        if not payment_id:
            raise ValueError("payment_id missing from order")

        checkout = _call(
            steps,
            "checkout_list",
            lambda: client.post(
                f"{GOPAY_CUSTOMER}/v2/customer/payment-options/checkout/list",
                body=build_checkout_body(order),
            ),
            options,
        )
        _require_status("checkout_list", checkout, {200})
        payment_token = extract_payment_token(checkout)

        last_used = _call(
            steps,
            "last_used",
            lambda: client.put(
                f"{GOPAY_CUSTOMER}/v1/customer/payment-options/settings/last-used",
                body={"token": payment_token},
            ),
            options,
        )
        _require_status("last_used", last_used, {200})

        capture_body = build_capture_body(order, payment_token)
        capture1 = _call(
            steps,
            "capture1",
            lambda: client.patch(
                f"{GOPAY_CUSTOMER}/v3/payments/{payment_id}/capture",
                body=capture_body,
                extra_headers={"Idempotency-Key": str(uuid.uuid1())},
            ),
            options,
        )
        challenge_id, client_id = extract_challenge(capture1)

        _call(
            steps,
            "pin_page",
            lambda: client.get(f"{GOPAY_CUSTOMER}/api/v2/challenges/{challenge_id}/pin-page"),
            options,
        )

        pin_resp = _call(
            steps,
            "pin_tokens",
            lambda: client.post(
                f"{GOPAY_CUSTOMER}/api/v1/users/pin/tokens",
                body={
                    "pin": options.pin,
                    "client_id": client_id,
                    "challenge_id": challenge_id,
                },
            ),
            options,
        )
        _require_status("pin_tokens", pin_resp, {200})
        pin_token = extract_pin_token(pin_resp)

        capture2_body = build_capture_body(
            order,
            payment_token,
            pin_token=pin_token,
            challenge_id=challenge_id,
            client_id=client_id,
        )
        capture2 = _call(
            steps,
            "capture2",
            lambda: client.patch(
                f"{GOPAY_CUSTOMER}/v3/payments/{payment_id}/capture",
                body=capture2_body,
                extra_headers={"Idempotency-Key": str(uuid.uuid1())},
            ),
            options,
        )
        data = capture2.get("data") if isinstance(capture2.get("data"), dict) else {}
        paid = capture2.get("status") == 200 and str(data.get("status") or "").upper() == "PAID"
        return {
            "success": bool(paid),
            "error_message": "" if paid else _response_error(capture2) or "payment not paid",
            "payment_id": payment_id,
            "status": str(data.get("status") or ""),
            "steps": steps,
        }
    except Exception as exc:
        return {
            "success": False,
            "error_message": str(exc),
            "payment_id": "",
            "status": "",
            "steps": steps,
        }


def build_qris_payment_body(qr_code: str, *, amount_value: int = 0, amount_currency: str = DEFAULT_CURRENCY) -> dict:
    qr_code = str(qr_code or "").strip()
    if not qr_code:
        raise ValueError("qr_code required")
    root = parse_emv_tlv(qr_code)
    merchant_account = parse_emv_tlv(root.get("26", ""))
    additional = parse_emv_tlv(root.get("62", ""))
    amount = int(amount_value or _parse_amount(root.get("54", "")) or 0)
    if amount <= 0:
        raise ValueError("amount_value required when QR does not contain amount")

    merchant_pan = merchant_account.get("01", "")
    merchant_id = merchant_account.get("02", "")
    merchant_criteria = merchant_account.get("03", "")
    acquirer_id = merchant_pan[:8] if len(merchant_pan) >= 8 else ""
    terminal_label = additional.get("07", "")
    custom_50 = additional.get("50", "")
    postal_code = root.get("61", "")
    additional_data_national = build_additional_data_national(postal_code, additional)
    currency_code = root.get("53", "")
    currency = amount_currency or currency_code_to_name(currency_code)
    merchant_name = root.get("59", "")
    merchant_city = root.get("60", "")
    category_code = root.get("52", "")
    country_code = root.get("58", "ID")
    channel_type = "DYNAMIC_QR" if root.get("01") == "12" else "STATIC_QR"

    aspi_qr_data = {
        "amount": amount,
        "postal_code": postal_code,
        "merchant_city": merchant_city,
        "merchant_id": merchant_id,
        "merchant_criteria": merchant_criteria,
        "merchant_pan": merchant_pan,
        "country_code": country_code,
        "transaction_currency_code": currency_code,
        "additional_data_national": additional_data_national,
        "additional_data": {
            "store_label": none_if_empty(additional.get("03", "")),
            "beneficiary_account_name": none_if_empty(additional.get("81", "")),
            "mobile_number": none_if_empty(additional.get("02", "")),
            "reference_label": none_if_empty(additional.get("05", "")),
            "customer_pan": None,
            "beneficiary_account_type": None,
            "purpose_of_transaction": none_if_empty(additional.get("08", "")),
            "customer_label": none_if_empty(additional.get("06", "")),
            "terminal_label": terminal_label or None,
            "bill_number": none_if_empty(additional.get("01", "")),
            "custom_50": custom_50 or None,
            "additional_consumer_data_request": none_if_empty(additional.get("09", "")),
            "customer_name": None,
            "loyalty_number": none_if_empty(additional.get("04", "")),
        },
        "merchant_category_code": category_code,
        "merchant_name": merchant_name,
        "trx_fee_amount": 0,
        "acquirer_id": acquirer_id,
    }
    now_ms = int(time.time() * 1000)
    qris_source_info = {
        "image": f"API_QR|TIME_OF_DOWNLOADED_IMAGE_IN_TIMESTAMP_MILLISECONDS:{now_ms}",
        "source": "API",
        "upload_time": str(now_ms),
    }
    return {
        "qr_code": qr_code,
        "amount": {"value": amount, "currency": currency},
        "channel_type": channel_type,
        "additional_data": {
            "merchant_order_id": "",
            "aspiqr_information": {
                "qr_transaction_type": "ON-US",
                "merchant_pan": merchant_pan,
                "transaction_currency_code": currency_code,
                "trx_fee_amount": 0,
                "retrieval_reference_number": "",
                "issuer_name": "gopay",
                "issuer_id": acquirer_id,
                "acquirer_name": "gopay",
                "acquirer_id": acquirer_id,
                "merchant_id": merchant_id,
                "merchant_name": merchant_name,
                "merchant_city": merchant_city,
                "merchant_criteria": merchant_criteria,
                "merchant_category_code": category_code,
                "postal_code": postal_code,
                "country_code": country_code,
                "additional_data_national": additional_data_national,
                "store_label": "",
                "mobile_number": "",
                "reference_label": "",
                "purpose_of_transaction": "",
                "customer_label": "",
                "terminal_label": terminal_label,
                "bill_number": "",
                "additional_consumer_data_request": "",
                "loyalty_number": "",
                "presentation_mode": "",
                "customer_pan": "",
                "customer_name": "",
                "beneficiary_account_name": "",
                "beneficiary_account_type": "DEFAULT",
            },
            "aspiqr_information_v2": {
                "network_participant": {
                    "issuer": {"id": acquirer_id, "name": "gopay"},
                    "acquirer": {"id": acquirer_id, "name": "gopay"},
                    "switch": {"id": "", "name": ""},
                },
                "account_participant": {"from_account": "", "to_account": merchant_pan},
                "customer_information": {"name": "", "language_preference": ""},
                "merchant_information": {
                    "id": merchant_id,
                    "name": merchant_name,
                    "city": merchant_city,
                    "postal_code": postal_code,
                    "criteria": merchant_criteria,
                    "category_code": category_code,
                    "country_code": country_code,
                    "terminal_id": terminal_label,
                    "sub_merchant_id": "",
                    "external_store_id": "",
                },
                "transaction_identification": {
                    "presentation_mode": "MPM",
                    "type": "ON-US",
                    "retrieval_reference_number": "",
                    "invoice_number": "",
                },
                "transaction_details": {
                    "amount": {"value": amount, "currency": currency},
                    "fee_amount": {"value": 0, "currency": ""},
                },
                "mpm_additional_data": {
                    "additional_data_national": additional_data_national,
                    "bill_number": "",
                    "mobile_number": "",
                    "store_label": "",
                    "loyalty_number": "",
                    "reference_label": "",
                    "customer_label": "",
                    "terminal_label": terminal_label,
                    "purpose_of_transaction": "",
                    "additional_consumer_data_request": "",
                },
                "cpm_additional_data": {
                    "qr_data": "",
                    "flow_type": "",
                    "completion_product_indicator": "",
                    "scanner_information": {"id": "", "version": "", "model": "", "ip_address": ""},
                },
            },
            "customer_flow": "qr",
        },
        "metadata": {
            "checksum": json_compact({"version": "3", "value": str(zlib.crc32(qr_code.encode()) & 0xffffffff)}),
            "merchant_cross_reference_id": str(uuid.uuid4()),
            "external_merchant_name": merchant_name,
            "payment_widget_intent": channel_type,
            "aspi_qr_data": json_compact(aspi_qr_data),
            "aspi_qr_transaction_type": "ON-US",
            "aspi_qr_issuer": "gopay",
            "aspi_qr_acquirer": "gopay",
            "customer_flow": "qr",
            "qris_source_info": json_compact(qris_source_info),
        },
    }


def build_checkout_body(order: dict[str, Any]) -> dict:
    amount = order_amount(order)
    merchant_id = order_merchant_id(order)
    service_id = order_service_id(order)
    return {
        "intent": order.get("payment_intent") or order.get("intent") or "EWALLET_QR",
        "order_pricing": {
            "payment_method_specific_pricing": [],
            "default_amount": {"amount": amount},
        },
        "selected_options_tokens": [],
        "merchant_id": merchant_id,
        "frontend_overrides": {
            "offline_methods": [],
            "payment_method_rollout": [],
            "exclude_paylater": False,
        },
        "service_id": service_id,
        "metadata": {
            "service_type": "QRIS",
            "service_id": service_id,
            "merchant_id": merchant_id,
        },
    }


def build_capture_body(
    order: dict[str, Any],
    payment_token: str,
    *,
    pin_token: str = "",
    challenge_id: str = "",
    client_id: str = "",
) -> dict:
    amount = order_amount(order)
    currency = order_currency(order)
    challenge = None
    if pin_token:
        challenge = {
            "action": None,
            "value": {"pin_token": pin_token},
            "type": "GOPAY_PIN_CHALLENGE",
            "metadata": {"challenge_id": challenge_id, "client_id": client_id},
        }
    return {
        "payment_instructions": [
            {
                "token": payment_token,
                "amount": {"value": amount, "currency": currency},
                "admin_fee_token": None,
            }
        ],
        "applied_promo_code": ["NO_PROMO_APPLIED"],
        "description": None,
        "payment_method": None,
        "channel_type": order.get("channel_type") or "ONLINE_GATEWAY",
        "additional_data": order.get("additional_data") or {},
        "challenge": challenge,
        "metadata": order.get("metadata") or {},
        "checksum": order.get("checksum") or parse_json_string((order.get("metadata") or {}).get("checksum")) or {},
        "order_signature": {
            "partner_id": "",
            "partner_name": "",
            "channel_type": "",
            "transaction_type": "",
            "reason": "",
            "customer_fulfillment_type": "",
            "source": "",
        },
    }


def parse_emv_tlv(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    pos = 0
    value = str(value or "")
    while pos + 4 <= len(value):
        tag = value[pos:pos + 2]
        raw_len = value[pos + 2:pos + 4]
        if not raw_len.isdigit():
            break
        length = int(raw_len)
        start = pos + 4
        end = start + length
        if end > len(value):
            break
        out[tag] = value[start:end]
        pos = end
    return out


def encode_tlv(items: list[tuple[str, str]]) -> str:
    return "".join(f"{tag}{len(value):02d}{value}" for tag, value in items if value is not None)


def build_additional_data_national(postal_code: str, additional: dict[str, str]) -> str:
    nested = encode_tlv(sorted((k, v) for k, v in additional.items() if v))
    parts = []
    if postal_code:
        parts.append(("61", postal_code))
    if nested:
        parts.append(("62", nested))
    return encode_tlv(parts)


def extract_payment_token(response: dict[str, Any]) -> str:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    for key in ("selected_options", "payment_options"):
        items = data.get(key) if isinstance(data.get(key), list) else []
        for item in items:
            token = str((item or {}).get("token") or "")
            if token:
                return token
    raise ValueError("payment option token missing")


def extract_challenge(response: dict[str, Any]) -> tuple[str, str]:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    challenge = data.get("challenge") if isinstance(data.get("challenge"), dict) else {}
    value = ((challenge.get("action") or {}).get("value") or {}) if isinstance(challenge, dict) else {}
    metadata = challenge.get("metadata") or {}
    challenge_id = str(value.get("challenge_id") or metadata.get("challenge_id") or "")
    client_id = str(value.get("client_id") or metadata.get("client_id") or "")
    if not challenge_id or not client_id:
        raise ValueError(f"capture challenge missing: {_response_error(response) or response.get('raw')}")
    return challenge_id, client_id


def extract_pin_token(response: dict[str, Any]) -> str:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    token = str(data.get("token") or data.get("pin_token") or "")
    if not token:
        raw = response.get("raw") if isinstance(response.get("raw"), dict) else {}
        token = str(raw.get("token") or "")
    if not token:
        raise ValueError("pin token missing")
    return token


def order_amount(order: dict[str, Any]) -> int:
    amount = order.get("amount") if isinstance(order.get("amount"), dict) else {}
    value = amount.get("value")
    if value is None:
        details = (((order.get("additional_data") or {}).get("aspiqr_information_v2") or {}).get("transaction_details") or {})
        nested = details.get("amount") if isinstance(details.get("amount"), dict) else {}
        value = nested.get("value")
    return int(float(value or 0))


def order_currency(order: dict[str, Any]) -> str:
    amount = order.get("amount") if isinstance(order.get("amount"), dict) else {}
    return str(amount.get("currency") or DEFAULT_CURRENCY)


def order_service_id(order: dict[str, Any]) -> str:
    widget = order.get("payment_widget_metadata") if isinstance(order.get("payment_widget_metadata"), dict) else {}
    metadata = order.get("metadata") if isinstance(order.get("metadata"), dict) else {}
    return str(order.get("service_id") or widget.get("service_id") or metadata.get("service_id") or DEFAULT_SERVICE_ID)


def order_merchant_id(order: dict[str, Any]) -> str:
    widget = order.get("payment_widget_metadata") if isinstance(order.get("payment_widget_metadata"), dict) else {}
    additional = order.get("additional_data") if isinstance(order.get("additional_data"), dict) else {}
    merchant = additional.get("merchant_information") if isinstance(additional.get("merchant_information"), dict) else {}
    aspi = additional.get("aspiqr_information") if isinstance(additional.get("aspiqr_information"), dict) else {}
    return str(widget.get("merchant_id") or merchant.get("id") or aspi.get("merchant_id") or "")


def _call(steps: list[dict[str, Any]], label: str, fn, options: QrPaymentOptions) -> dict[str, Any]:
    response = fn()
    steps.append(step_result(label, response, options))
    return response


def step_result(label: str, response: dict[str, Any], options: QrPaymentOptions) -> dict[str, Any]:
    raw = response.get("raw")
    text = json_compact(raw) if isinstance(raw, (dict, list)) else str(raw or response.get("data") or "")
    return {
        "label": label,
        "status_code": int(response.get("status") or 0),
        "response_text": text[: max(int(options.body_limit or 1200), 1)],
        "error_message": _response_error(response),
    }


def _require_status(label: str, response: dict[str, Any], statuses: set[int]) -> None:
    status = int(response.get("status") or 0)
    if status not in statuses:
        raise ValueError(f"{label} failed: status={status} {_response_error(response)}")


def _response_error(response: dict[str, Any]) -> str:
    raw = response.get("raw") if isinstance(response.get("raw"), dict) else {}
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    errors = raw.get("errors") or data.get("errors") or []
    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
        code = str(first.get("code") or "")
        message = str(first.get("message") or first.get("message_title") or "")
        return f"{code} {message}".strip()
    if response.get("status") == 0:
        return str(data.get("error") or raw.get("error") or "")
    return ""


def _load_order(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("order_json must be a JSON object")
    if isinstance(value.get("data"), dict):
        value = value["data"]
    return value


def _parse_amount(value: str) -> int:
    if not value:
        return 0
    return int(float(value))


def currency_code_to_name(code: str) -> str:
    return "IDR" if str(code or "") == "360" else DEFAULT_CURRENCY


def json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def parse_json_string(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return value


def none_if_empty(value: str) -> str | None:
    return value or None
