"""Keep execution ownership locked through every signed mutation and response."""

import sys

from v2_core.errors import SubmissionNotSent
from v2_core.transport import ExchangeTransportError


class GuardedSignedRequest:
    def __init__(self, request, *, account_id, environment, guard):
        if (request.account_id, request.environment) != (
            account_id,
            environment,
        ) or not callable(guard):
            raise ValueError("EXPLICIT_GUARDED_TRANSPORT_BINDING_REQUIRED")
        self.account_id, self.environment = account_id, environment
        self.request, self.guard = request, guard

    @staticmethod
    def operation(method, path, params):
        if method == "DELETE" and path in {"/fapi/v1/order", "/fapi/v1/algoOrder"}:
            return "MANAGE"
        if (
            method == "POST"
            and path == "/fapi/v1/order"
            and isinstance(params, dict)
            and params.get("reduceOnly") == "true"
        ):
            return "MANAGE"
        if (
            method == "POST"
            and path == "/fapi/v1/algoOrder"
            and isinstance(params, dict)
            and params.get("closePosition") == "true"
        ):
            return "MANAGE"
        return "OPEN"

    def __call__(self, method, path, params):
        if method == "GET":
            return self.request(method, path, params)
        try:
            context = self.guard(operation=self.operation(method, path, params))
            context.__enter__()
        except Exception:  # noqa: BLE001 - no write was invoked; never leak DB/token diagnostics
            raise SubmissionNotSent("EXECUTION_FENCED") from None
        try:
            return self.request(method, path, params)
        finally:
            # Never reinterpret a lost lock/commit AFTER I/O as a safe-to-retry write.
            try:
                context.__exit__(*sys.exc_info())
            except Exception:  # noqa: BLE001 - external outcome must be reconciled
                raise ExchangeTransportError("NETWORK_OUTCOME_UNKNOWN") from None
