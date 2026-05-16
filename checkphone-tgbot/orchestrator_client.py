import os


DEFAULT_ORCHESTRATOR_ADDR = "orchestrator:50051"


class OrchestratorGopayClient:
    def __init__(self, addr: str = "", *, timeout: int = 120):
        self.addr = str(addr or os.environ.get("ORCHESTRATOR_ADDR") or DEFAULT_ORCHESTRATOR_ADDR).strip()
        self.timeout = max(1, int(timeout or 120))
        self._pb2 = None
        self._stub = None

    def _ensure_stub(self):
        if self._stub is not None:
            return self._pb2, self._stub
        import grpc
        import orchestrator_pb2
        import orchestrator_pb2_grpc

        channel = grpc.insecure_channel(self.addr)
        self._pb2 = orchestrator_pb2
        self._stub = orchestrator_pb2_grpc.OrchestratorServiceStub(channel)
        return self._pb2, self._stub

    def status(self, state_key: str):
        pb2, stub = self._ensure_stub()
        return stub.GoPayUserStatus(pb2.GoPayUserStatusRequest(state_key=state_key), timeout=self.timeout)

    def clear_state(self, state_key: str):
        pb2, stub = self._ensure_stub()
        return stub.GoPayUserClearState(pb2.GoPayUserClearStateRequest(state_key=state_key), timeout=self.timeout)

    def auth_start(self, state_key: str, *, phone: str, country_code: str, pin: str):
        pb2, stub = self._ensure_stub()
        return stub.GoPayUserAuthStart(
            pb2.GoPayUserAuthStartRequest(
                state_key=state_key,
                phone=phone,
                country_code=country_code,
                pin=pin,
            ),
            timeout=self.timeout,
        )

    def auth_complete(self, state_key: str, *, otp: str, pin: str):
        pb2, stub = self._ensure_stub()
        return stub.GoPayUserAuthComplete(
            pb2.GoPayUserAuthCompleteRequest(state_key=state_key, otp=otp, pin=pin),
            timeout=self.timeout,
        )

    def change_phone_start(self, state_key: str, *, new_phone: str, pin: str):
        pb2, stub = self._ensure_stub()
        return stub.GoPayUserChangePhoneStart(
            pb2.GoPayUserChangePhoneStartRequest(state_key=state_key, new_phone=new_phone, pin=pin),
            timeout=self.timeout,
        )

    def change_phone_complete(self, state_key: str, *, otp: str):
        pb2, stub = self._ensure_stub()
        return stub.GoPayUserChangePhoneComplete(
            pb2.GoPayUserChangePhoneCompleteRequest(state_key=state_key, otp=otp),
            timeout=self.timeout,
        )

    def change_phone_retry(self, state_key: str):
        pb2, stub = self._ensure_stub()
        return stub.GoPayUserChangePhoneRetry(
            pb2.GoPayUserChangePhoneRetryRequest(state_key=state_key),
            timeout=self.timeout,
        )

    def signup_start(self, state_key: str, *, phone: str, name: str, email: str, country_code: str):
        pb2, stub = self._ensure_stub()
        return stub.GoPayUserSignupStart(
            pb2.GoPayUserSignupStartRequest(
                state_key=state_key,
                phone=phone,
                name=name,
                email=email,
                country_code=country_code,
            ),
            timeout=self.timeout,
        )

    def signup_complete(self, state_key: str, *, otp: str):
        pb2, stub = self._ensure_stub()
        return stub.GoPayUserSignupComplete(
            pb2.GoPayUserSignupCompleteRequest(state_key=state_key, otp=otp),
            timeout=self.timeout,
        )

    def create_pin_start(self, state_key: str, *, pin: str):
        pb2, stub = self._ensure_stub()
        return stub.GoPayUserCreatePinStart(
            pb2.GoPayUserCreatePinStartRequest(state_key=state_key, pin=pin),
            timeout=self.timeout,
        )

    def create_pin_complete(self, state_key: str, *, otp: str, pin: str):
        pb2, stub = self._ensure_stub()
        return stub.GoPayUserCreatePinComplete(
            pb2.GoPayUserCreatePinCompleteRequest(state_key=state_key, otp=otp, pin=pin),
            timeout=self.timeout,
        )
