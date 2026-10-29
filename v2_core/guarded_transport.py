"""Keep execution ownership locked through every signed mutation and response."""

import sys

from v2_core.errors import SubmissionNotSent
from v2_core.reduction_fence import revalidate_reduction_write
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

    def __call__(self, method, path, params, *, admission_ticket=None):
        if method == "GET":
            if admission_ticket is not None:
                return self.request(
                    method, path, params, admission_ticket=admission_ticket
                )
            return self.request(method, path, params)
        if admission_ticket is not None:
            raise ValueError("ADMISSION_CONTEXT_TICKET_INVALID")
        try:
            context = self.guard(operation=self.operation(method, path, params))
            context.__enter__()
        except Exception as exc:  # noqa: BLE001 - no write invoked; only a fixed safe code
            raise SubmissionNotSent(
                "EXECUTION_GUARD_PERMISSION_DENIED"
                if getattr(exc, "sqlstate", None) == "42501"
                else "EXECUTION_FENCED"
            ) from None
        try:
            revalidate_reduction_write(
                self.account_id, self.environment, method, path, params
            )
            return self.request(method, path, params)
        finally:
            # Never reinterpret a lost lock/commit AFTER I/O as a safe-to-retry write.
            try:
                context.__exit__(*sys.exc_info())
            except Exception:  # noqa: BLE001 - external outcome must be reconciled
                raise ExchangeTransportError("NETWORK_OUTCOME_UNKNOWN") from None
