"""Audited account liquidity gates without changing historical entry controls.

Version zero means the exact OFF defaults. These controls never change capital
ceilings or grant execution authority. Writes share the opening submission lock.
"""

import json
import re
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal
from uuid import uuid4

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.state import BusinessState, StateKey

_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_BPS_LIMITS = {
    "max_spread_bps": Decimal(100),
    "max_impact_bps": Decimal(100),
    "max_price_basis_bps": Decimal(500),
}


def defaults():
    return {
        "mode": "OFF",
        "max_age_ms": 5000,
        "max_execution_quote_age_ms": 30000,
        "max_spread_bps": "20",
        "max_impact_bps": "15",
        "max_price_basis_bps": "100",
    }


def resolve(changes):
    values = defaults()
    if not isinstance(changes, dict) or set(changes) - set(values):
        raise ValueError("LIQUIDITY_CONTROLS_UNKNOWN_FIELD")
    values.update(deepcopy(changes))
    if not isinstance(values["mode"], str) or values["mode"] not in {
        "OFF",
        "SHADOW",
        "ENFORCE",
    }:
        raise ValueError("LIQUIDITY_CONTROLS_INVALID_MODE")
    if (
        type(values["max_age_ms"]) is not int
        or not 100 <= values["max_age_ms"] <= 15000
    ):
        raise ValueError("LIQUIDITY_CONTROLS_INVALID_AGE")
    if (
        type(values["max_execution_quote_age_ms"]) is not int
        or not 100 <= values["max_execution_quote_age_ms"] <= 60000
    ):
        raise ValueError("LIQUIDITY_CONTROLS_INVALID_EXECUTION_QUOTE_AGE")
    # Ordinary ASCII decimal strings only: no floats, exponents or whitespace.
    # Caps prevent accidental percent/bps confusion; they are not calibrated alpha.
    for name, maximum in _BPS_LIMITS.items():
        value = values[name]
        if (
            not isinstance(value, str)
            or len(value) > 32
            or _DECIMAL.fullmatch(value) is None
        ):
            raise ValueError("LIQUIDITY_CONTROLS_INVALID_BPS")
        number = Decimal(value)
        if not number.is_finite() or not 0 < number <= maximum:
            raise ValueError("LIQUIDITY_CONTROLS_INVALID_BPS")
    return values


def _account(scope):
    account = asdict(scope) if isinstance(scope, AccountScope) else scope
    try:
        if not isinstance(account, dict):
            raise TypeError()
        return asdict(AccountScope(**account))
    except (TypeError, ValueError):
        raise ValueError("LIQUIDITY_CONTROLS_SCOPE_INVALID") from None


def _reason(reason):
    if (
        not isinstance(reason, str)
        or not 5 <= len(reason.strip()) <= 240
        or any(ord(c) < 32 or ord(c) == 127 for c in reason)
    ):
        raise ValueError("LIQUIDITY_CONTROLS_REASON_REQUIRED")
    return reason.strip()


@dataclass(frozen=True)
class Controls:
    version: int
    values: dict
    digest: str

    def snapshot(self):
        return {
            "version": self.version,
            "values": deepcopy(self.values),
            "digest": self.digest,
        }


def from_snapshot(saved, scope):
    """Verify the frozen account configuration without reading current state."""
    if not isinstance(saved, dict) or set(saved) != {"version", "values", "digest"}:
        raise ValueError("LIQUIDITY_CONTROLS_PROOF_INVALID")
    version = saved["version"]
    if type(version) is not int or version < 0:
        raise ValueError("LIQUIDITY_CONTROLS_PROOF_INVALID")
    values = resolve(saved["values"])
    if values != saved["values"] or (version == 0 and values != defaults()):
        raise ValueError("LIQUIDITY_CONTROLS_PROOF_INVALID")
    account = _account(scope)
    if version and account["environment"] != "SANDBOX":
        raise ValueError("LIQUIDITY_CONTROLS_TESTNET_ONLY")
    fingerprint = digest(
        canonical({"scope": account, "version": version, "values": values})
    )
    if saved["digest"] != fingerprint:
        raise ValueError("LIQUIDITY_CONTROLS_PROOF_INVALID")
    return Controls(version, values, fingerprint)


def key(scope):
    return StateKey(**_account(scope), namespace="liquidity-controls-v1", key="active")


def read(connect, scope):
    row = BusinessState(connect).read(key(scope))
    if row is not None and row.deleted:
        raise ValueError("LIQUIDITY_CONTROLS_DELETED")
    values, version = defaults(), 0
    if row is not None:
        payload = json.loads(row.payload_json)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"values", "reason"}
            or not isinstance(payload["values"], dict)
            or set(payload["values"]) != set(defaults())
            or type(row.version) is not int
            or row.version < 1
        ):
            raise ValueError("LIQUIDITY_CONTROLS_STATE_INVALID")
        _reason(payload["reason"])
        values, version = resolve(payload["values"]), row.version
    snapshot = {
        "version": version,
        "values": values,
        "digest": digest(
            canonical({"scope": _account(scope), "version": version, "values": values})
        ),
    }
    return from_snapshot(snapshot, scope)


def configure(connect, scope, changes, *, expected_version, reason, request_key=None):
    if _account(scope)["environment"] != "SANDBOX":
        raise ValueError("LIQUIDITY_CONTROLS_TESTNET_ONLY")
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("LIQUIDITY_CONTROLS_VERSION_REQUIRED")
    reason = _reason(reason)
    if not isinstance(changes, dict):
        raise TypeError("LIQUIDITY_CONTROLS_UNKNOWN_FIELD")
    if request_key is None:
        request_key = str(uuid4())
    if (
        not isinstance(request_key, str)
        or not 1 <= len(request_key) <= 240
        or request_key != request_key.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in request_key)
    ):
        raise ValueError("LIQUIDITY_CONTROLS_REQUEST_KEY_REQUIRED")
    with connect() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + canonical(_account(scope)),),
        )
        connect_here = lambda: nullcontext(conn)
        old = read(connect_here, scope)
        if old.version != expected_version:
            raise ValueError("LIQUIDITY_CONTROLS_VERSION_CONFLICT")
        values = resolve({**old.values, **changes})
        from v2_core.runtime_policy import PolicyStore

        policy = (
            PolicyStore(connect_here, AccountScope(**_account(scope))).read().values
        )
        if not all(
            policy[name]
            for name in (
                "capital.enabled",
                "capital.model_enabled",
                "portfolio_risk.enabled",
            )
        ):
            raise ValueError("LIQUIDITY_CONTROLS_REQUIRE_CAPITAL_GUARDS")
        changed = BusinessState(connect_here).change(
            key(scope),
            expected_version=expected_version,
            request_key=request_key,
            payload={"values": values, "reason": reason},
            reason="TESTNET_LIQUIDITY_CONTROLS_CHANGED",
        )
        if changed.code != "APPLIED":
            raise ValueError("LIQUIDITY_CONTROLS_VERSION_CONFLICT")
        return read(connect_here, scope)


def validate_submission(conn, scope, order_id):
    """Require a new proposal after any change, under the account submit lock."""
    current = read(lambda: nullcontext(conn), scope)
    from v2_core.strategy_execution_context import read_execution_context

    try:
        typed = read_execution_context(conn, scope, order_id)
    except ValueError as exc:
        if exc.args == ("STRATEGY_EXECUTION_ORDER_MISSING",):
            raise ValueError("LIQUIDITY_CONTROLS_ORDER_MISSING") from None
        raise
    if typed is not None:
        _, context = typed
        if context["liquidity_controls"] != current.snapshot():
            raise ValueError("LIQUIDITY_CONTROLS_CHANGED_REPROPOSE")
        return typed
    account = _account(scope)
    row = conn.execute(
        "SELECT e.snapshot#>'{features,context,liquidity_controls}' FROM v2_orders o "
        "JOIN v2_trade_intents i ON i.intent_id=o.episode_id "
        "JOIN v2_decision_evidence e USING(evidence_ref) WHERE o.order_id=%s "
        "AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)",
        (order_id, *account.values()),
    ).fetchone()
    if row is None:
        raise ValueError("LIQUIDITY_CONTROLS_ORDER_MISSING")
    saved = row[0]
    if saved is None and current.version == 0:
        return
    if saved != current.snapshot():
        raise ValueError("LIQUIDITY_CONTROLS_CHANGED_REPROPOSE")
