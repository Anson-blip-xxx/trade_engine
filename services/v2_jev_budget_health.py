"""Read-only advisory quota/model diagnostics; never a trading gate or model call.

This reports the local shared input-token allowance, not provider credit. A
planned pacing pause is not exhaustion. No claim, budget or trading row is
created here; the watchdog owns its existing notification/deduplication state.
Model completions use a bounded, fixed-key projection, separately from quota.
"""

from contextlib import nullcontext

from services.v2_jev_coordinator import (
    DAY_NAMESPACE,
    _budget,
    _budget_key,
    _clock,
    _read,
    _window_starts,
)
from services.v2_jev_entry_context import policy_key
from services.v2_jev_reference import CONTROL_KEY
from v2_core.account_risk import AccountScope
from v2_core.jev_advisory import RESERVE_TOKENS, AdvisoryPolicy
from v2_core.jev_entry_gate import JevEntryGatePolicy
from v2_core.jev_health import (
    MODEL_HEALTH_KEY,
    MODEL_HEALTH_POLICY_KEY,
    model_health_diagnostic,
    model_health_policy,
)
from v2_core.state import BusinessState

CODE = "JEV_LOCAL_DAILY_BUDGET_EXHAUSTED"
MODEL_CODE = "JEV_MODEL_DEGRADED"


def read_budget_health(connect, scope):
    """A bounded, fresh read; UNKNOWN must not clear an existing incident."""
    if not isinstance(scope, AccountScope) or (
        scope.exchange,
        scope.environment,
        scope.product,
    ) != ("BINANCE", "SANDBOX", "FUTURES"):
        raise ValueError("JEV_HEALTH_SANDBOX_SCOPE_REQUIRED")
    try:
        with connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute("SET LOCAL statement_timeout='100ms'")
            conn.execute("SET LOCAL lock_timeout='50ms'")
            store = BusinessState(lambda: nullcontext(conn))
            _, payload = _read(store, policy_key(scope))
            if payload is None:
                return {"status": "NOT_CONFIGURED"}
            gate = (
                JevEntryGatePolicy(**payload)
                if payload is not None
                else JevEntryGatePolicy()
            )
            if not gate.enabled:
                return {"status": "DISABLED"}
            _, control = _read(store, CONTROL_KEY)
            if control is None:
                return {"status": "UNKNOWN"}
            policy = (
                AdvisoryPolicy(**control) if control is not None else AdvisoryPolicy()
            )
            if not policy.enabled:
                return {"status": "DISABLED"}
            now = _clock(conn)
            _, day = _window_starts(now)
            _, saved = _read(store, _budget_key(DAY_NAMESPACE, day))
            budget = _budget(
                saved,
                kind="utc8-day",
                start=day,
                limit=min(500000, policy.tokens_per_day),
            )
            # Two exact primary-key reads, never a recent-request table scan.
            # The independent projection is optional: its failure must not
            # erase a valid local-budget observation or other safety findings.
            try:
                with conn.transaction():
                    _, health_policy = _read(store, MODEL_HEALTH_POLICY_KEY)
                    settings = model_health_policy(health_policy)
                    _, snapshot = _read(store, MODEL_HEALTH_KEY)
                    model_health = model_health_diagnostic(
                        snapshot, now_ms=now, **settings
                    )
            except Exception:  # noqa: BLE001 - no raw DB/config details in alerts
                model_health = {"status": "UNKNOWN"}
            return {
                "status": "EXHAUSTED"
                if budget["used"] + RESERVE_TOKENS > budget["limit"]
                else "AVAILABLE",
                "observed_at_ms": now,
                "day_start_ms": day,
                "reset_at_ms": day + 86400000,
                "used_tokens": budget["used"],
                "limit_tokens": budget["limit"],
                "reservation_tokens": RESERVE_TOKENS,
                "unavailable_action": gate.unavailable_action,
                "scope": "SHARED_LOCAL_INPUT_TOKEN_ALLOWANCE",
                "budget_row_present": saved is not None,
                "model_health": model_health,
            }
    except Exception:  # noqa: BLE001 - diagnostic failure must not hide other alerts
        return {"status": "UNKNOWN"}


def apply_budget_health(findings, previous, diagnostic):
    """No repeats while exhausted, and no false recovery on an unreadable DB."""
    previous = previous if isinstance(previous, dict) else {}
    model_health = diagnostic.get("model_health")
    if type(model_health) is not dict:
        model_health = {
            "status": "DISABLED"
            if diagnostic.get("status") == "DISABLED"
            else "UNKNOWN"
        }
    apply_model_health(findings, previous, model_health)
    active = previous.get("active")
    was_active = isinstance(active, list) and CODE in active
    old_findings = previous.get("findings")
    old_finding = old_findings.get(CODE) if isinstance(old_findings, dict) else None
    old_finding = old_finding if isinstance(old_finding, dict) else {}
    old_details = old_finding.get("details")
    old_details = old_details if isinstance(old_details, dict) else {}
    old_day = old_finding.get("budget_day_start_ms", old_details.get("day_start_ms"))
    missing_same_or_unknown_day = (
        was_active
        and diagnostic.get("status") == "AVAILABLE"
        and diagnostic.get("budget_row_present") is False
        and (type(old_day) is not int or diagnostic.get("day_start_ms") == old_day)
    )
    if missing_same_or_unknown_day:
        diagnostic = {
            "status": "UNKNOWN",
            "reason": "BUDGET_ROW_MISSING_AFTER_EXHAUSTION",
        }
    if diagnostic.get("status") == "EXHAUSTED":
        findings[CODE] = {
            "details": {**diagnostic, "status": "CURRENT"},
            "budget_day_start_ms": diagnostic["day_start_ms"],
        }
    elif diagnostic.get("status") in {"UNKNOWN", "NOT_CONFIGURED"} and was_active:
        findings[CODE] = {
            "details": {"status": "UNAVAILABLE"},
            "awaiting_fresh_read": True,
            "budget_day_start_ms": old_day,
        }
    return diagnostic


def apply_model_health(findings, previous, diagnostic):
    """Existing watchdog dedup owns delivery; no requests is never recovery.

    Only a strictly later validated successful completion clears an incident.
    Budget reset, disabled policy, missing projection and database outages are
    not evidence that the model resumed working.
    """
    previous = previous if type(previous) is dict else {}
    active = previous.get("active")
    was_active = type(active) is list and MODEL_CODE in active
    old_findings = previous.get("findings")
    old = old_findings.get(MODEL_CODE) if type(old_findings) is dict else None
    old = old if type(old) is dict else {}
    last_failure = old.get("last_failure_at_ms")
    last_failure = last_failure if type(last_failure) is int else None
    diagnostic = diagnostic if type(diagnostic) is dict else {"status": "UNKNOWN"}
    status = diagnostic.get("status")
    if status == "DEGRADED":
        # model_health_diagnostic only exports whitelisted scalar data. Copy
        # this allowlist again so notification callers cannot carry raw text.
        fields = (
            "observed_at_ms",
            "latest_result_at_ms",
            "last_success_at_ms",
            "last_failure_at_ms",
            "consecutive_failures",
            "recent_requests",
            "recent_failures",
            "window_ms",
            "latest_reason",
            "reason",
        )
        details = {name: diagnostic[name] for name in fields if name in diagnostic}
        findings[MODEL_CODE] = {
            "details": {**details, "status": "CURRENT"},
            "last_failure_at_ms": diagnostic.get("last_failure_at_ms"),
        }
    elif was_active:
        success = diagnostic.get("last_success_at_ms")
        if (
            status == "HEALTHY"
            and type(success) is int
            and (last_failure is None or success > last_failure)
        ):
            return diagnostic
        findings[MODEL_CODE] = {
            "details": {"status": "UNAVAILABLE"},
            "awaiting_fresh_success": True,
            "last_failure_at_ms": last_failure,
        }
    return diagnostic
