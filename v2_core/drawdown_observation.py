"""Explicit Testnet-only drawdown observation, never a capital/risk bypass."""

import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from uuid import uuid4

from v2_core.state import BusinessState, StateKey


def control(connect, scope):
    row = BusinessState(connect).read(
        StateKey(**asdict(scope), namespace="capital-drawdown-control-v1", key="active")
    )
    if row is None:
        return True, 0
    if row.deleted:
        raise ValueError("DRAWDOWN_CONTROL_DELETED")
    value = json.loads(row.payload_json)["guard_enabled"]
    observation_only({"capital.drawdown_guard_enabled": value}, scope.environment)
    return value, row.version


def configure(connect, scope, *, enabled, expected_version, reason):
    """Explicit operator-only, audited CAS; no generic web state write grant."""
    from v2_core.runtime_policy import PolicyStore

    if scope.environment != "SANDBOX" or type(enabled) is not bool:
        raise ValueError("DRAWDOWN_OBSERVATION_TESTNET_ONLY")
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 240:
        raise ValueError("DRAWDOWN_CONTROL_REASON_REQUIRED")
    with connect() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + scope.key,),
        )
        settings = PolicyStore(lambda: nullcontext(conn), scope).read().values
        if not enabled and not all(
            settings[k]
            for k in (
                "capital.enabled",
                "capital.model_enabled",
                "portfolio_risk.enabled",
            )
        ):
            raise ValueError("DRAWDOWN_OBSERVATION_REQUIRES_CAPITAL_GUARDS")
        result = BusinessState(lambda: nullcontext(conn)).change(
            StateKey(
                **asdict(scope), namespace="capital-drawdown-control-v1", key="active"
            ),
            expected_version=expected_version,
            request_key=str(uuid4()),
            payload={"guard_enabled": enabled, "reason": reason.strip()},
            reason="TESTNET_DRAWDOWN_CONTROL_CHANGED",
        )
        if result.code != "APPLIED":
            raise ValueError("DRAWDOWN_CONTROL_VERSION_CONFLICT")
        return result.version


def observation_only(settings, environment):
    enabled = settings.get("capital.drawdown_guard_enabled", True)
    if type(enabled) is not bool:
        raise ValueError("INVALID_DRAWDOWN_GUARD_MODE")
    if not enabled and environment != "SANDBOX":
        raise ValueError("DRAWDOWN_OBSERVATION_TESTNET_ONLY")
    return not enabled


def observe(result, previous):
    previous = previous or {}
    suspended = deepcopy(previous.get("suspended_recovery"))
    if suspended is None:
        suspended = deepcopy(
            previous.get("recovery", {"mode": "ACTIVE", "attempts": 0})
        )
    return {
        **result,
        "factor": "1",
        "drawdown_guard_enabled": False,
        "recovery": {"mode": "OBSERVE_ONLY", "attempts": suspended.get("attempts", 0)},
        "suspended_recovery": suspended,
    }


def restored_previous(previous):
    if not previous or "suspended_recovery" not in previous:
        return previous
    state = deepcopy(previous["suspended_recovery"])
    # Old probe outcomes/cooldown clocks must not absorb observation-only trades
    # and silently authorize another automatic recovery when protection returns.
    if state.get("mode") in {"PROBE", "PAUSED"}:
        state["mode"] = "HALTED"
    return {**previous, "recovery": state}


def sizing_factor(legacy_factor, capital_health, settings, environment):
    if observation_only(settings, environment):
        return Decimal(1)
    return min(
        Decimal(legacy_factor),
        Decimal(capital_health["factor"]) if capital_health else Decimal(1),
    )
