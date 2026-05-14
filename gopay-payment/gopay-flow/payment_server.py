#!/usr/bin/env python3
"""Segmented gRPC wrapper for the GoPay payment flow."""

from __future__ import annotations

import argparse
import copy
import logging
import os
import threading
import uuid
from concurrent import futures
from dataclasses import dataclass
from typing import Any

import grpc

import account_db_pb2
import account_db_pb2_grpc
import gopay_cycle_pb2
import gopay_cycle_pb2_grpc
import payment_pb2
import payment_pb2_grpc
from gopay import (
    DEFAULT_STRIPE_PK,
    GoPayCharger,
    GoPayError,
    OTPCancelled,
    _build_chatgpt_session,
    _load_cfg,
    probe_plus_active_session_token,
    probe_plus_trial_checkout,
    resolve_gopay_cfg,
    resolve_checkout_proxy,
    resolve_payment_proxy,
    validate_gopay_cfg,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _billing_from_config(cfg: dict[str, Any]) -> dict[str, Any]:
    billing = cfg.get("billing") or {}
    if billing:
        return dict(billing)
    cards = cfg.get("cards") or []
    if cards and isinstance(cards[0], dict):
        card0 = cards[0]
        out = dict(card0.get("address") or {})
        for key in ("name", "email"):
            if card0.get(key):
                out[key] = card0[key]
        return out
    return {}


def _normalize_listen(value: str) -> str:
    value = (value or ":50051").strip()
    if value.startswith(":"):
        return "[::]" + value
    return value


def _close_session(session: Any) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _looks_access_token(value: str) -> bool:
    # OpenAI access tokens are JWTs. NextAuth session tokens are JWE-like and
    # normally have a different dot count, so this is only a compatibility shim.
    return value.count(".") == 2


def _requires_manual_payment_confirmation(flow: "PendingFlow") -> bool:
    tokenization = str(getattr(flow.charger, "midtrans_tokenization", "true") or "true").strip().lower()
    return tokenization == "false"


@dataclass
class PendingFlow:
    charger: GoPayCharger
    state: dict[str, Any]
    use_cycle_token: bool = False

    def close(self) -> None:
        self.charger.close()


class FlowStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._flows: dict[str, PendingFlow] = {}

    def put(self, charger: GoPayCharger, state: dict[str, Any], use_cycle_token: bool = False) -> str:
        flow_id = uuid.uuid4().hex
        with self._lock:
            self._flows[flow_id] = PendingFlow(charger=charger, state=state, use_cycle_token=use_cycle_token)
        return flow_id

    def get(self, flow_id: str) -> PendingFlow | None:
        with self._lock:
            return self._flows.get(flow_id)

    def pop(self, flow_id: str) -> PendingFlow | None:
        with self._lock:
            return self._flows.pop(flow_id, None)

    def close(self) -> None:
        with self._lock:
            flows = list(self._flows.values())
            self._flows.clear()
        for flow in flows:
            flow.close()


class PaymentService(payment_pb2_grpc.PaymentServiceServicer):
    def __init__(self, cfg: dict[str, Any]):
        self._cfg = cfg
        self._flows = FlowStore()
        self._cycle_addr = os.environ.get("GOPAY_CYCLE_ADDR", "gopay-cycle:50051").strip()
        self._account_db_addr = os.environ.get("ACCOUNT_DB_ADDR", "account-db:50051").strip()
        self._cycle_state_key = os.environ.get("GOPAY_CYCLE_STATE_KEY", "default").strip() or "default"

    def close(self) -> None:
        self._flows.close()

    def _ready_cycle_access_token(self) -> tuple[str, str]:
        if not self._cycle_addr:
            raise GoPayError("GOPAY_CYCLE_ADDR is required when use_cycle_token=true")
        channel = grpc.insecure_channel(self._cycle_addr)
        try:
            state_json = self._load_cycle_state()
            resp = gopay_cycle_pb2_grpc.GopayCycleServiceStub(channel).GetReadyAccessToken(
                gopay_cycle_pb2.GetReadyAccessTokenRequest(state_json=state_json),
                timeout=10,
            )
            self._save_cycle_state(getattr(resp, "state_json", ""))
        finally:
            channel.close()
        if not resp or not resp.success or not (resp.access_token or "").strip():
            message = resp.error_message if resp else "empty gopay-cycle response"
            raise GoPayError(f"cycle token not ready: {message}")
        return resp.access_token.strip(), str(resp.phone or "")

    def _load_cycle_state(self) -> str:
        if not self._account_db_addr:
            raise GoPayError("ACCOUNT_DB_ADDR is required for cycle state")
        channel = grpc.insecure_channel(self._account_db_addr)
        try:
            resp = account_db_pb2_grpc.AccountDatabaseServiceStub(channel).GetGoPayCycleState(
                account_db_pb2.GetGoPayCycleStateRequest(state_key=self._cycle_state_key),
                timeout=10,
            )
        finally:
            channel.close()
        state_json = (resp.state.state_json if resp and resp.state else "").strip()
        return state_json or "{}"

    def _save_cycle_state(self, state_json: str) -> None:
        state_json = (state_json or "").strip()
        if not state_json:
            return
        if not self._account_db_addr:
            raise GoPayError("ACCOUNT_DB_ADDR is required for cycle state")
        channel = grpc.insecure_channel(self._account_db_addr)
        try:
            account_db_pb2_grpc.AccountDatabaseServiceStub(channel).UpsertGoPayCycleState(
                account_db_pb2.UpsertGoPayCycleStateRequest(
                    state=account_db_pb2.GoPayCycleState(
                        state_key=self._cycle_state_key,
                        state_json=state_json,
                    )
                ),
                timeout=10,
            )
        finally:
            channel.close()

    def ProbeTier(self, request, context):
        session_token = (request.session_token or "").strip()
        if not session_token:
            return payment_pb2.ProbeTierPaymentResponse(
                success=False,
                error_message="session_token is required",
            )
        try:
            proxy = resolve_checkout_proxy(self._cfg)
            result = probe_plus_active_session_token(
                session_token,
                proxy=proxy,
                log=logger.info,
            )
            return payment_pb2.ProbeTierPaymentResponse(
                success=not bool(result.get("error_message")),
                error_message=str(result.get("error_message") or "")[:500],
                checked=bool(result.get("checked")),
                tier=str(result.get("tier") or result.get("plan_type") or ""),
                plus_active=bool(result.get("plus_active")),
                source=str(result.get("source") or "auth_session"),
            )
        except Exception as exc:
            logger.exception("[payment] ProbeTier crashed")
            return payment_pb2.ProbeTierPaymentResponse(success=False, error_message=str(exc)[:500])

    def ProbePlusTrial(self, request, context):
        session_token = (request.session_token or "").strip()
        access_token = (getattr(request, "access_token", "") or "").strip()
        if session_token and not access_token and _looks_access_token(session_token):
            access_token = session_token
            session_token = ""

        if not (session_token or access_token):
            return payment_pb2.ProbePlusTrialPaymentResponse(
                success=False,
                error_message="session_token or access_token is required",
            )

        cs_session = None
        try:
            cfg = copy.deepcopy(self._cfg)
            fresh_checkout = cfg.get("fresh_checkout") or {}
            auth_cfg = dict(fresh_checkout.get("auth") or {})
            auth_cfg.pop("cookie_header", None)
            auth_cfg.pop("session_token", None)
            auth_cfg.pop("access_token", None)
            if session_token:
                auth_cfg["session_token"] = session_token
            if access_token:
                auth_cfg["access_token"] = access_token

            proxy = resolve_checkout_proxy(cfg)
            session_probe = None
            if session_token:
                logger.info("[payment] ProbePlusSession start")
                session_probe = probe_plus_active_session_token(
                    session_token,
                    proxy=proxy,
                    log=logger.info,
                )
                if session_probe.get("checked") and session_probe.get("plus_active"):
                    logger.info(
                        "[payment] ProbePlusSession active plan=%s source=%s",
                        session_probe.get("plan_type"),
                        session_probe.get("source"),
                    )
                    return payment_pb2.ProbePlusTrialPaymentResponse(
                        success=True,
                        checked=True,
                        plus_trial_eligible=False,
                        source=str(session_probe.get("source") or "auth_session"),
                        plus_active=True,
                        plan_type=str(session_probe.get("tier") or session_probe.get("plan_type") or ""),
                    )
                if session_probe.get("error_message") and not access_token:
                    return payment_pb2.ProbePlusTrialPaymentResponse(
                        success=False,
                        checked=bool(session_probe.get("checked")),
                        error_message=str(session_probe.get("error_message") or "")[:500],
                        source=str(session_probe.get("source") or "auth_session"),
                        plus_active=False,
                        plan_type=str(session_probe.get("tier") or session_probe.get("plan_type") or ""),
                    )

            cs_session = _build_chatgpt_session(auth_cfg, proxy=proxy)
            stripe_pk = (
                (cfg.get("stripe") or {}).get("publishable_key")
                or auth_cfg.get("stripe_pk")
                or DEFAULT_STRIPE_PK
            )

            logger.info("[payment] ProbePlusTrial start")
            result = probe_plus_trial_checkout(
                cs_session,
                stripe_pk=stripe_pk,
                runtime_cfg=dict(cfg.get("runtime") or {}),
                checkout_cfg=dict((cfg.get("fresh_checkout") or {}).get("plan") or {}),
                proxy=proxy,
                log=logger.info,
            )
            logger.info(
                "[payment] ProbePlusTrial checked=%s eligible=%s amount=%s source=%s",
                result.get("checked"),
                result.get("plus_trial_eligible"),
                result.get("amount"),
                result.get("source"),
            )
            return payment_pb2.ProbePlusTrialPaymentResponse(
                success=True,
                error_message=str(result.get("error_message") or "")[:500],
                checked=bool(result.get("checked")),
                plus_trial_eligible=bool(result.get("plus_trial_eligible")),
                amount=int(result.get("amount") or 0),
                currency=str(result.get("currency") or ""),
                source=str(result.get("source") or ""),
                checkout_url=str(result.get("checkout_url") or ""),
                plus_active=bool(session_probe and session_probe.get("plus_active")),
                plan_type=str((session_probe or {}).get("tier") or (session_probe or {}).get("plan_type") or ""),
            )
        except GoPayError as exc:
            logger.error("[payment] ProbePlusTrial failed: %s", exc)
            return payment_pb2.ProbePlusTrialPaymentResponse(success=False, error_message=str(exc)[:500])
        except Exception as exc:
            logger.exception("[payment] ProbePlusTrial crashed")
            return payment_pb2.ProbePlusTrialPaymentResponse(success=False, error_message=str(exc)[:500])
        finally:
            if cs_session is not None:
                _close_session(cs_session)

    def StartGoPay(self, request, context):
        session_token = (request.session_token or "").strip()
        access_token = (getattr(request, "access_token", "") or "").strip()
        use_cycle_token = bool(getattr(request, "use_cycle_token", False))
        tokenization = (getattr(request, "tokenization", "") or "").strip()
        cycle_phone = ""
        if use_cycle_token:
            try:
                _, cycle_phone = self._ready_cycle_access_token()
                logger.info("[payment] Using internal gopay-cycle token phone=%s", cycle_phone)
            except Exception as exc:
                logger.error("[payment] cycle token unavailable: %s", exc)
                return payment_pb2.StartGoPayResponse(success=False, error_message=str(exc)[:500])
        elif session_token and not access_token and _looks_access_token(session_token):
            access_token = session_token
            session_token = ""

        if not (session_token or access_token):
            return payment_pb2.StartGoPayResponse(
                success=False,
                error_message="session_token or access_token is required",
            )

        charger = None
        cs_session = None
        try:
            cfg = copy.deepcopy(self._cfg)
            fresh_checkout = cfg.get("fresh_checkout") or {}
            auth_cfg = dict(fresh_checkout.get("auth") or {})
            auth_cfg.pop("cookie_header", None)
            auth_cfg.pop("session_token", None)
            auth_cfg.pop("access_token", None)
            if session_token:
                auth_cfg["session_token"] = session_token
            if access_token:
                auth_cfg["access_token"] = access_token

            checkout_proxy = resolve_checkout_proxy(cfg)
            payment_proxy = resolve_payment_proxy(cfg)
            cs_session = _build_chatgpt_session(auth_cfg, proxy=checkout_proxy)

            gopay_cfg = resolve_gopay_cfg(cfg)
            if use_cycle_token and cycle_phone:
                gopay_cfg["phone_number"] = cycle_phone
            if tokenization:
                gopay_cfg["tokenization"] = tokenization
            gopay_cfg = validate_gopay_cfg(gopay_cfg)

            stripe_pk = (
                (cfg.get("stripe") or {}).get("publishable_key")
                or auth_cfg.get("stripe_pk")
                or DEFAULT_STRIPE_PK
            )
            runtime_cfg = dict(cfg.get("runtime") or {})
            charger = GoPayCharger(
                cs_session,
                gopay_cfg,
                otp_provider=lambda: (_ for _ in ()).throw(OTPCancelled("external OTP required")),
                checkout_proxy=checkout_proxy,
                payment_proxy=payment_proxy,
                runtime_cfg=runtime_cfg,
                checkout_cfg=dict((cfg.get("fresh_checkout") or {}).get("plan") or {}),
                browser_challenge_cfg=dict(cfg.get("browser_challenge") or {}),
                pre_solve_passive_captcha=bool(cfg.get("pre_solve_passive_captcha", False)),
                log=logger.info,
            )

            logger.info("[payment] StartGoPay start")
            state = charger.start_until_otp(stripe_pk=stripe_pk, billing=_billing_from_config(cfg))
            flow_id = self._flows.put(charger, state, use_cycle_token=use_cycle_token)
            charger = None
            cs_session = None
            logger.info("[payment] StartGoPay waiting_otp flow=%s", flow_id[:8])
            return payment_pb2.StartGoPayResponse(
                success=True,
                flow_id=flow_id,
                snap_token=str(state.get("snap_token") or ""),
                issued_after_unix=int(state.get("issued_after_unix") or 0),
                expires_at_unix=0,
            )
        except GoPayError as exc:
            logger.error("[payment] StartGoPay failed: %s", exc)
            return payment_pb2.StartGoPayResponse(success=False, error_message=str(exc)[:500])
        except Exception as exc:
            logger.exception("[payment] StartGoPay crashed")
            return payment_pb2.StartGoPayResponse(success=False, error_message=str(exc)[:500])
        finally:
            if charger is not None:
                charger.close()
            elif cs_session is not None:
                _close_session(cs_session)

    def CompleteGoPay(self, request, context):
        if not request.flow_id:
            return payment_pb2.GoPayResponse(success=False, error_message="flow_id is required")
        if not request.otp:
            return payment_pb2.GoPayResponse(success=False, error_message="otp is required")

        flow = self._flows.get(request.flow_id)
        if flow is None:
            return payment_pb2.GoPayResponse(success=False, error_message="payment flow not found")

        close_flow = True
        try:
            logger.info("[payment] CompleteGoPay flow=%s", request.flow_id[:8])
            if _requires_manual_payment_confirmation(flow):
                result = flow.charger.complete_after_otp_until_manual_confirmation(flow.state, request.otp)
                flow.state = result
                close_flow = False
                return payment_pb2.GoPayResponse(
                    success=True,
                    awaiting_manual_confirmation=True,
                    charge_ref=str(result.get("charge_ref") or ""),
                    snap_token=str(result.get("snap_token") or ""),
                    deeplink_url=str(result.get("deeplink_url") or ""),
                    qr_code_url=str(result.get("qr_code_url") or ""),
                    finish_redirect_url=str(result.get("finish_redirect_url") or ""),
                    finish_200_redirect_url=str(result.get("finish_200_redirect_url") or ""),
                )

            result = flow.charger.complete_after_otp(flow.state, request.otp)
            state = str(result.get("state") or "")
            success = state == "succeeded"
            if success and flow.use_cycle_token:
                try:
                    self._unlink_cycle_token()
                except Exception as exc:
                    logger.error("[payment] cycle unlink failed after payment success: %s", exc)
            return payment_pb2.GoPayResponse(
                success=success,
                error_message="" if success else f"payment state={state or 'unknown'}",
                charge_ref=str(result.get("charge_ref") or ""),
                snap_token=str(result.get("snap_token") or ""),
                deeplink_url=str(result.get("deeplink_url") or ""),
                qr_code_url=str(result.get("qr_code_url") or ""),
                finish_redirect_url=str(result.get("finish_redirect_url") or ""),
                finish_200_redirect_url=str(result.get("finish_200_redirect_url") or ""),
            )
        except GoPayError as exc:
            logger.error("[payment] CompleteGoPay failed: %s", exc)
            return payment_pb2.GoPayResponse(success=False, error_message=str(exc)[:500])
        except Exception as exc:
            logger.exception("[payment] CompleteGoPay crashed")
            return payment_pb2.GoPayResponse(success=False, error_message=str(exc)[:500])
        finally:
            if close_flow:
                flow = self._flows.pop(request.flow_id)
                if flow is not None:
                    flow.close()

    def ConfirmGoPayPayment(self, request, context):
        if not request.flow_id:
            return payment_pb2.GoPayResponse(success=False, error_message="flow_id is required")

        flow = self._flows.pop(request.flow_id)
        if flow is None:
            return payment_pb2.GoPayResponse(success=False, error_message="payment flow not found")

        try:
            logger.info("[payment] ConfirmGoPayPayment flow=%s", request.flow_id[:8])
            result = flow.charger.complete_after_manual_confirmation(flow.state)
            state = str(result.get("state") or "")
            success = state == "succeeded"
            if success and flow.use_cycle_token:
                try:
                    self._unlink_cycle_token()
                except Exception as exc:
                    logger.error("[payment] cycle unlink failed after payment success: %s", exc)
            return payment_pb2.GoPayResponse(
                success=success,
                error_message="" if success else f"payment state={state or 'unknown'}",
                charge_ref=str(result.get("charge_ref") or ""),
                snap_token=str(result.get("snap_token") or ""),
                deeplink_url=str(result.get("deeplink_url") or ""),
                qr_code_url=str(result.get("qr_code_url") or ""),
                finish_redirect_url=str(result.get("finish_redirect_url") or ""),
                finish_200_redirect_url=str(result.get("finish_200_redirect_url") or ""),
            )
        except GoPayError as exc:
            logger.error("[payment] ConfirmGoPayPayment failed: %s", exc)
            return payment_pb2.GoPayResponse(success=False, error_message=str(exc)[:500])
        except Exception as exc:
            logger.exception("[payment] ConfirmGoPayPayment crashed")
            return payment_pb2.GoPayResponse(success=False, error_message=str(exc)[:500])
        finally:
            flow.close()

    def _unlink_cycle_token(self) -> None:
        if not self._cycle_addr:
            raise GoPayError("GOPAY_CYCLE_ADDR is required for cycle unlink")
        channel = grpc.insecure_channel(self._cycle_addr)
        try:
            state_json = self._load_cycle_state()
            resp = gopay_cycle_pb2_grpc.GopayCycleServiceStub(channel).Unlink(
                gopay_cycle_pb2.UnlinkRequest(state_json=state_json),
                timeout=15,
            )
            self._save_cycle_state(getattr(resp, "state_json", ""))
        finally:
            channel.close()
        if not resp or not resp.success:
            message = resp.error_message if resp else "empty gopay-cycle response"
            raise GoPayError(message)
        logger.info("[payment] cycle token unlinked count=%s", resp.unlinked_count)

    def CancelGoPay(self, request, context):
        if not request.flow_id:
            return payment_pb2.CancelGoPayResponse(success=False, error_message="flow_id is required")
        flow = self._flows.pop(request.flow_id)
        if flow is not None:
            flow.close()
        logger.info("[payment] CancelGoPay flow=%s found=%s", request.flow_id[:8], flow is not None)
        return payment_pb2.CancelGoPayResponse(success=True)


def serve(config_path: str, listen: str):
    cfg = _load_cfg(config_path)
    service = PaymentService(cfg)
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[
            ("grpc.max_send_message_length", 50 * 1024 * 1024),
            ("grpc.max_receive_message_length", 50 * 1024 * 1024),
        ],
    )
    payment_pb2_grpc.add_PaymentServiceServicer_to_server(service, server)
    listen_addr = _normalize_listen(listen)
    server.add_insecure_port(listen_addr)
    server.start()
    logger.info("[payment] gRPC listening on %s", listen_addr)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("[payment] shutting down")
        server.stop(grace=5)
    finally:
        service.close()


def main():
    parser = argparse.ArgumentParser(description="GoPay payment gRPC service")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--listen", default=":50051")
    parser.add_argument("--flow-ttl", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()

    serve(
        config_path=args.config,
        listen=args.listen,
    )


if __name__ == "__main__":
    main()
