"""
HeroSMS code receiver gRPC service.

The service is stateless for activations. Provider credentials and defaults are
loaded from Postgres on each operation and can be managed through gRPC CRUD.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent import futures

import grpc
import code_receiver_pb2
import code_receiver_pb2_grpc

DEFAULT_API_BASE = "https://hero-sms.com/stubs/handler_api.php"
PORT = int(os.environ.get("SMS_PORT", os.environ.get("CODE_RECEIVER_PORT", "50051")))
PG_DSN = (
    os.environ.get("CODE_RECEIVER_PG_DSN", "").strip()
    or os.environ.get("PG_DSN", "").strip()
)
CONFIG_TABLE = os.environ.get("CODE_RECEIVER_CONFIG_TABLE", "code_receiver_provider_configs").strip() or "code_receiver_provider_configs"
DEFAULT_CONFIG_ID = os.environ.get("CODE_RECEIVER_DEFAULT_CONFIG_ID", "default").strip() or "default"
_CONFIG_STORE = None
_CONFIG_STORE_LOCK = threading.RLock()
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STATUS_OK = {
    1: "ACCESS_READY",
    3: "ACCESS_RETRY_GET",
    6: "ACCESS_ACTIVATION",
    8: "ACCESS_CANCEL",
}
_COUNTRY_CALLING_CODES = {
    0: "7",
    1: "380",
    2: "7",
    3: "86",
    4: "63",
    5: "95",
    6: "62",
    7: "60",
    8: "254",
    10: "84",
    16: "44",
    22: "91",
    36: "1",
    43: "49",
    52: "66",
    73: "55",
    78: "33",
    117: "351",
    187: "1",
    196: "65",
}


def _json_result(result: str):
    try:
        return json.loads(result)
    except (TypeError, ValueError):
        return None


def _result_error(result: str) -> str:
    data = _json_result(result)
    if isinstance(data, dict):
        title = data.get("title") or data.get("error") or data.get("status") or data.get("message")
        detail = data.get("details") or data.get("message") or data.get("error")
        if title and detail and title != detail:
            return f"{title}: {detail}"
        if title:
            return str(title)
    return result


def _normalize_config_id(value: str) -> str:
    value = str(value or "").strip()
    return value or DEFAULT_CONFIG_ID


def _normalize_provider(value: str) -> str:
    value = str(value or "").strip().lower()
    return value or "herosms"


def _validate_table_name(value: str) -> str:
    if not _IDENTIFIER_RE.match(value):
        raise RuntimeError("CODE_RECEIVER_CONFIG_TABLE must be a simple SQL identifier")
    return value


def _config_to_pb(row) -> code_receiver_pb2.CodeReceiverProviderConfig:
    if isinstance(row, code_receiver_pb2.CodeReceiverProviderConfig):
        return row
    return code_receiver_pb2.CodeReceiverProviderConfig(
        config_id=str(row.get("config_id") or ""),
        provider=str(row.get("provider") or ""),
        enabled=bool(row.get("enabled")),
        api_base=str(row.get("api_base") or ""),
        api_key=str(row.get("api_key") or ""),
        proxy=str(row.get("proxy") or ""),
        default_service=str(row.get("default_service") or ""),
        default_country=int(row.get("default_country") or 0),
        default_country_calling_code=str(row.get("default_country_calling_code") or ""),
        default_max_price=float(row.get("default_max_price") or 0),
        created_at_unix=int(row.get("created_at_unix") or 0),
        updated_at_unix=int(row.get("updated_at_unix") or 0),
    )


def _config_from_pb(config: code_receiver_pb2.CodeReceiverProviderConfig) -> code_receiver_pb2.CodeReceiverProviderConfig:
    provider = _normalize_provider(config.provider)
    if provider != "herosms":
        raise ValueError(f"unsupported code receiver provider: {provider}")
    return code_receiver_pb2.CodeReceiverProviderConfig(
        config_id=_normalize_config_id(config.config_id),
        provider=provider,
        enabled=bool(config.enabled),
        api_base=(config.api_base or DEFAULT_API_BASE).strip() or DEFAULT_API_BASE,
        api_key=str(config.api_key or "").strip(),
        proxy=str(config.proxy or "").strip(),
        default_service=(config.default_service or "ni").strip() or "ni",
        default_country=int(config.default_country or 6),
        default_country_calling_code=str(config.default_country_calling_code or "62").strip().lstrip("+") or "62",
        default_max_price=float(config.default_max_price or 0.05),
        created_at_unix=int(config.created_at_unix or 0),
        updated_at_unix=int(config.updated_at_unix or 0),
    )


class PostgresProviderConfigStore:
    def __init__(self, dsn: str, table: str):
        self.dsn = dsn
        self.table = _validate_table_name(table)
        self._ready = False
        self._bootstrapped = False
        self._lock = threading.RLock()

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("psycopg is required for herosms-sms-service provider config persistence") from exc
        return psycopg.connect(self.dsn, autocommit=True)

    def _ensure_table(self):
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            with self._connect() as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.table} (
                        config_id TEXT PRIMARY KEY,
                        provider TEXT NOT NULL,
                        enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        api_base TEXT NOT NULL,
                        api_key TEXT NOT NULL,
                        proxy TEXT NOT NULL DEFAULT '',
                        default_service TEXT NOT NULL DEFAULT '',
                        default_country INTEGER NOT NULL DEFAULT 0,
                        default_country_calling_code TEXT NOT NULL DEFAULT '',
                        default_max_price DOUBLE PRECISION NOT NULL DEFAULT 0,
                        created_at BIGINT NOT NULL,
                        updated_at BIGINT NOT NULL
                    )
                    """
                )
            self._ready = True

    def _bootstrap_from_env(self):
        if self._bootstrapped:
            return
        with self._lock:
            if self._bootstrapped:
                return
            self._ensure_table()
            if not os.environ.get("HEROSMS_API_KEY", "").strip():
                self._bootstrapped = True
                return
            if self.get(DEFAULT_CONFIG_ID):
                self._bootstrapped = True
                return
            self.upsert(
                code_receiver_pb2.CodeReceiverProviderConfig(
                    config_id=DEFAULT_CONFIG_ID,
                    provider="herosms",
                    enabled=True,
                    api_base=os.environ.get("HEROSMS_API_BASE", DEFAULT_API_BASE).strip() or DEFAULT_API_BASE,
                    api_key=os.environ.get("HEROSMS_API_KEY", "").strip(),
                    proxy=os.environ.get("SMS_PROXY", "").strip(),
                    default_service=os.environ.get("HEROSMS_SERVICE", "ni").strip() or "ni",
                    default_country=int(os.environ.get("HEROSMS_COUNTRY", "6") or "6"),
                    default_country_calling_code=os.environ.get("HEROSMS_COUNTRY_CALLING_CODE", "62").strip().lstrip("+") or "62",
                    default_max_price=float(os.environ.get("HEROSMS_MAX_PRICE", "0.05") or "0.05"),
                )
            )
            self._bootstrapped = True

    def upsert(self, config: code_receiver_pb2.CodeReceiverProviderConfig) -> code_receiver_pb2.CodeReceiverProviderConfig:
        config = _config_from_pb(config)
        now = int(time.time())
        created_at = config.created_at_unix or now
        updated_at = now
        self._ensure_table()
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {self.table} (
                    config_id, provider, enabled, api_base, api_key, proxy,
                    default_service, default_country, default_country_calling_code,
                    default_max_price, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (config_id) DO UPDATE
                SET provider = EXCLUDED.provider,
                    enabled = EXCLUDED.enabled,
                    api_base = EXCLUDED.api_base,
                    api_key = EXCLUDED.api_key,
                    proxy = EXCLUDED.proxy,
                    default_service = EXCLUDED.default_service,
                    default_country = EXCLUDED.default_country,
                    default_country_calling_code = EXCLUDED.default_country_calling_code,
                    default_max_price = EXCLUDED.default_max_price,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    config.config_id,
                    config.provider,
                    config.enabled,
                    config.api_base,
                    config.api_key,
                    config.proxy,
                    config.default_service,
                    config.default_country,
                    config.default_country_calling_code,
                    config.default_max_price,
                    created_at,
                    updated_at,
                ),
            )
        config.created_at_unix = created_at
        config.updated_at_unix = updated_at
        return config

    def get(self, config_id: str):
        self._ensure_table()
        config_id = _normalize_config_id(config_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT config_id, provider, enabled, api_base, api_key, proxy,
                       default_service, default_country, default_country_calling_code,
                       default_max_price, created_at, updated_at
                FROM {self.table}
                WHERE config_id = %s
                """,
                (config_id,),
            ).fetchone()
        if not row:
            return None
        return code_receiver_pb2.CodeReceiverProviderConfig(
            config_id=row[0],
            provider=row[1],
            enabled=row[2],
            api_base=row[3],
            api_key=row[4],
            proxy=row[5],
            default_service=row[6],
            default_country=row[7],
            default_country_calling_code=row[8],
            default_max_price=row[9],
            created_at_unix=row[10],
            updated_at_unix=row[11],
        )

    def list(self, include_disabled: bool):
        self._bootstrap_from_env()
        self._ensure_table()
        where = "" if include_disabled else "WHERE enabled = TRUE"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT config_id, provider, enabled, api_base, api_key, proxy,
                       default_service, default_country, default_country_calling_code,
                       default_max_price, created_at, updated_at
                FROM {self.table}
                {where}
                ORDER BY config_id
                """
            ).fetchall()
        return [
            code_receiver_pb2.CodeReceiverProviderConfig(
                config_id=row[0],
                provider=row[1],
                enabled=row[2],
                api_base=row[3],
                api_key=row[4],
                proxy=row[5],
                default_service=row[6],
                default_country=row[7],
                default_country_calling_code=row[8],
                default_max_price=row[9],
                created_at_unix=row[10],
                updated_at_unix=row[11],
            )
            for row in rows
        ]

    def delete(self, config_id: str) -> bool:
        self._ensure_table()
        with self._connect() as conn:
            result = conn.execute(f"DELETE FROM {self.table} WHERE config_id = %s", (_normalize_config_id(config_id),))
        return result.rowcount > 0


def _config_store() -> PostgresProviderConfigStore:
    global _CONFIG_STORE
    if not PG_DSN:
        raise RuntimeError("CODE_RECEIVER_PG_DSN or PG_DSN is required")
    with _CONFIG_STORE_LOCK:
        if _CONFIG_STORE is None:
            _CONFIG_STORE = PostgresProviderConfigStore(PG_DSN, CONFIG_TABLE)
        return _CONFIG_STORE


def _load_provider_config(config_id: str) -> code_receiver_pb2.CodeReceiverProviderConfig:
    store = _config_store()
    store._bootstrap_from_env()
    config = store.get(config_id)
    if not config:
        raise RuntimeError(f"code receiver provider config not found: {_normalize_config_id(config_id)}")
    if not config.enabled:
        raise RuntimeError(f"code receiver provider config disabled: {config.config_id}")
    if _normalize_provider(config.provider) != "herosms":
        raise RuntimeError(f"unsupported code receiver provider: {config.provider}")
    if not config.api_key:
        raise RuntimeError(f"api_key is required for code receiver provider config: {config.config_id}")
    return config


def _calling_code(config: code_receiver_pb2.CodeReceiverProviderConfig, country: int) -> str:
    if country == config.default_country and config.default_country_calling_code:
        return config.default_country_calling_code
    return _COUNTRY_CALLING_CODES.get(country, "")


def _normalize_phone(config: code_receiver_pb2.CodeReceiverProviderConfig, phone: str, country: int) -> str:
    value = str(phone or "").strip().lstrip("+")
    prefix = _calling_code(config, country)
    if prefix and value.startswith(prefix):
        return value[len(prefix):]
    return value


def _provider_call(config: code_receiver_pb2.CodeReceiverProviderConfig, action: str, **params) -> str:
    params["api_key"] = config.api_key
    params["action"] = action
    url = f"{(config.api_base or DEFAULT_API_BASE).strip()}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    if config.proxy:
        handler = urllib.request.ProxyHandler({"https": config.proxy, "http": config.proxy})
        opener = urllib.request.build_opener(handler)
    else:
        opener = urllib.request.build_opener()
    try:
        resp = opener.open(req, timeout=15)
        return resp.read().decode().strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode().strip()
        if body:
            return body
        raise


def _set_status(config: code_receiver_pb2.CodeReceiverProviderConfig, activation_id: str, status: int) -> tuple[bool, str]:
    if not activation_id:
        return False, "activation_id required"
    if status not in _STATUS_OK:
        return False, "unsupported status"
    result = _provider_call(config, "setStatus", id=activation_id, status=status)
    expected = _STATUS_OK.get(status)
    return (not expected or result == expected), result


def _action_response(config, response_type, activation_id: str, status: int):
    success, result = _set_status(config, activation_id, status)
    return response_type(
        success=success,
        error_message="" if success else _result_error(result),
        raw_response=result,
    )


class CodeReceiverServicer(code_receiver_pb2_grpc.CodeReceiverServiceServicer):
    def UpsertProvider(self, request, context):
        try:
            config = _config_store().upsert(request.config)
            return code_receiver_pb2.UpsertCodeReceiverProviderResponse(success=True, config=config)
        except Exception as e:
            return code_receiver_pb2.UpsertCodeReceiverProviderResponse(success=False, error_message=str(e))

    def GetProvider(self, request, context):
        try:
            config = _config_store().get(request.config_id)
            if not config:
                return code_receiver_pb2.GetCodeReceiverProviderResponse(success=False, error_message="provider config not found")
            return code_receiver_pb2.GetCodeReceiverProviderResponse(success=True, config=config)
        except Exception as e:
            return code_receiver_pb2.GetCodeReceiverProviderResponse(success=False, error_message=str(e))

    def ListProviders(self, request, context):
        try:
            return code_receiver_pb2.ListCodeReceiverProvidersResponse(
                success=True,
                configs=_config_store().list(request.include_disabled),
            )
        except Exception as e:
            return code_receiver_pb2.ListCodeReceiverProvidersResponse(success=False, error_message=str(e))

    def DeleteProvider(self, request, context):
        try:
            deleted = _config_store().delete(request.config_id)
            if not deleted:
                return code_receiver_pb2.DeleteCodeReceiverProviderResponse(success=False, error_message="provider config not found")
            return code_receiver_pb2.DeleteCodeReceiverProviderResponse(success=True)
        except Exception as e:
            return code_receiver_pb2.DeleteCodeReceiverProviderResponse(success=False, error_message=str(e))

    def AcquireNumber(self, request, context):
        try:
            config = _load_provider_config(request.config_id)
            service = (request.service or config.default_service).strip() or config.default_service
            country = request.country or config.default_country
            params = {"service": service, "country": country}
            max_price = request.max_price or config.default_max_price
            if max_price > 0:
                params["maxPrice"] = max_price

            result = _provider_call(config, "getNumber", **params)
            if result.startswith("ACCESS_NUMBER"):
                parts = result.split(":", 2)
                if len(parts) != 3:
                    return code_receiver_pb2.AcquireNumberResponse(success=False, error_message=f"bad ACCESS_NUMBER response: {result}")
                activation_id = parts[1]
                raw_phone = parts[2]
                phone = _normalize_phone(config, raw_phone, country)
                prefix = _calling_code(config, country)
                display_phone = f"+{prefix}{phone}" if prefix else raw_phone
                print(f"[herosms-sms] AcquireNumber config={config.config_id} service={service} country={country}: {display_phone} id={activation_id}")
                return code_receiver_pb2.AcquireNumberResponse(
                    success=True,
                    activation_id=activation_id,
                    phone=phone,
                    provider=config.provider,
                )
            error = _result_error(result)
            print(f"[herosms-sms] AcquireNumber failed config={config.config_id} service={service}: {error}")
            return code_receiver_pb2.AcquireNumberResponse(success=False, error_message=error, provider=config.provider)
        except Exception as e:
            return code_receiver_pb2.AcquireNumberResponse(success=False, error_message=str(e))

    def WaitCode(self, request, context):
        try:
            if not request.activation_id:
                return code_receiver_pb2.WaitCodeResponse(success=False, error_message="activation_id required")
            config = _load_provider_config(request.config_id)
            timeout = request.timeout_seconds or 120
            deadline = time.time() + timeout
            while time.time() < deadline:
                if context is not None and not context.is_active():
                    return code_receiver_pb2.WaitCodeResponse(success=False, error_message="cancelled")
                result = _provider_call(config, "getStatus", id=request.activation_id)
                if result.startswith("STATUS_OK"):
                    parts = result.split(":", 1)
                    code = parts[1].strip() if len(parts) == 2 else ""
                    if not code:
                        return code_receiver_pb2.WaitCodeResponse(success=False, error_message=f"bad STATUS_OK response: {result}")
                    print(f"[herosms-sms] WaitCode: got code {code} for {request.activation_id}")
                    return code_receiver_pb2.WaitCodeResponse(success=True, code=code)
                if result == "STATUS_CANCEL":
                    print(f"[herosms-sms] WaitCode: cancelled {request.activation_id}")
                    return code_receiver_pb2.WaitCodeResponse(success=False, error_message="cancelled")
                if result == "STATUS_WAIT_CODE" or result.startswith("STATUS_WAIT_RETRY"):
                    time.sleep(min(5, max(0.0, deadline - time.time())))
                    continue
                return code_receiver_pb2.WaitCodeResponse(success=False, error_message=_result_error(result))
            return code_receiver_pb2.WaitCodeResponse(success=False, error_message="timeout")
        except Exception as e:
            return code_receiver_pb2.WaitCodeResponse(success=False, error_message=str(e))

    def MarkMessageSent(self, request, context):
        try:
            config = _load_provider_config(request.config_id)
            return _action_response(config, code_receiver_pb2.ProviderActionResponse, request.activation_id, 1)
        except Exception as e:
            return code_receiver_pb2.ProviderActionResponse(success=False, error_message=str(e))

    def RequestAdditionalCode(self, request, context):
        try:
            config = _load_provider_config(request.config_id)
            return _action_response(config, code_receiver_pb2.ProviderActionResponse, request.activation_id, 3)
        except Exception as e:
            return code_receiver_pb2.ProviderActionResponse(success=False, error_message=str(e))

    def FinishActivation(self, request, context):
        try:
            config = _load_provider_config(request.config_id)
            return _action_response(config, code_receiver_pb2.ProviderActionResponse, request.activation_id, 6)
        except Exception as e:
            return code_receiver_pb2.ProviderActionResponse(success=False, error_message=str(e))

    def CancelActivation(self, request, context):
        try:
            config = _load_provider_config(request.config_id)
            return _action_response(config, code_receiver_pb2.ProviderActionResponse, request.activation_id, 8)
        except Exception as e:
            return code_receiver_pb2.ProviderActionResponse(success=False, error_message=str(e))

    def GetProviderBalance(self, request, context):
        try:
            config = _load_provider_config(request.config_id)
            result = _provider_call(config, "getBalance")
            if result.startswith("ACCESS_BALANCE:"):
                result = result.split(":", 1)[1]
            return code_receiver_pb2.GetProviderBalanceResponse(success=True, balance=_result_error(result))
        except Exception as e:
            return code_receiver_pb2.GetProviderBalanceResponse(success=False, error_message=str(e))


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    code_receiver_pb2_grpc.add_CodeReceiverServiceServicer_to_server(CodeReceiverServicer(), server)
    server.add_insecure_port(f"0.0.0.0:{PORT}")
    server.start()
    print(f"[herosms-sms-service] gRPC listening on :{PORT}")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
