"""Audited per-account entry experiments, separate from shared policy schema.

Absent controls preserve old behavior. Only explicit SANDBOX operator changes
are accepted. No credential, risk ceiling or capital baseline belongs here.
"""

import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from v2_core.evidence import canonical, digest
from v2_core.state import BusinessState, StateKey

_CYCLE_FIELDS = frozenset({"reentry.gate_scope", "reentry.cycle_policy"})


def defaults():
    from v2_core.reentry import ReentryPolicy
    from v2_core.trade_cycle import CyclePolicy

    return {
        "sizing.remaining_budget_enabled": False,
        "sizing.price_drift_fraction": "0.015",
        "history.mode": "OFF",
        "history.lookback_days": 14,
        "history.min_samples": 30,
        "history.prior_weight": 20,
        "history.soft_factor": "0.75",
        "history.max_samples": 500,
        "reentry.mode": "OFF",
        "reentry.enforce_reasons": ["STRUCTURE_CHASE_TOO_FAR"],
        "reentry.enforce_sides": ["BUY"],
        "reentry.max_age_ms": 30000,
        "reentry.max_bar_age_ms": 90000,
        "reentry.max_minutes": 720,
        "reentry.policy": asdict(ReentryPolicy()),
        "reentry.gate_scope": "CHASE_ONLY",
        "reentry.cycle_policy": asdict(CyclePolicy()),
    }


def resolve(changes):
    values = defaults()
    if not isinstance(changes, dict) or set(changes) - set(values):
        raise ValueError("ENTRY_CONTROLS_UNKNOWN_FIELD")
    values.update(deepcopy(changes))
    if type(values["sizing.remaining_budget_enabled"]) is not bool:
        raise ValueError("ENTRY_CONTROLS_INVALID_FLAG")
    for key, choices in (
        ("history.mode", {"OFF", "SHADOW", "SOFT"}),
        ("reentry.mode", {"OFF", "SHADOW", "ENFORCE"}),
        ("reentry.gate_scope", {"CHASE_ONLY", "TRADE_CYCLE"}),
    ):
        if not isinstance(values[key], str) or values[key] not in choices:
            raise ValueError("ENTRY_CONTROLS_INVALID_MODE")
    for key, low, high in (
        ("history.lookback_days", 1, 90),
        ("history.min_samples", 20, 500),
        ("history.prior_weight", 1, 1000),
        ("history.max_samples", 50, 1000),
        ("reentry.max_age_ms", 1000, 60000),
        ("reentry.max_bar_age_ms", 60000, 180000),
        ("reentry.max_minutes", 120, 1440),
    ):
        if type(values[key]) is not int or not low <= values[key] <= high:
            raise ValueError("ENTRY_CONTROLS_INVALID_BOUND")
    if values["history.min_samples"] > values["history.max_samples"]:
        raise ValueError("ENTRY_CONTROLS_SAMPLE_BOUND")
    for key, low, high in (
        ("sizing.price_drift_fraction", Decimal(0), Decimal("0.05")),
        ("history.soft_factor", Decimal("0.25"), Decimal(1)),
    ):
        try:
            if not isinstance(values[key], str) or len(values[key]) > 32:
                raise ValueError()
            number = Decimal(values[key])
            if not number.is_finite() or not low <= number <= high:
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise ValueError("ENTRY_CONTROLS_INVALID_FRACTION") from None
    for key, allowed in (
        ("reentry.enforce_reasons", {"STRUCTURE_CHASE_TOO_FAR"}),
        ("reentry.enforce_sides", {"BUY", "SELL"}),
    ):
        items = values[key]
        if (
            not isinstance(items, list)
            or not items
            or any(not isinstance(x, str) or x not in allowed for x in items)
            or len(items) != len(set(items))
        ):
            raise ValueError("ENTRY_CONTROLS_INVALID_CANARY")
    from v2_core.reentry import ReentryPolicy

    try:
        if not isinstance(values["reentry.policy"], dict):
            raise TypeError()
        policy = ReentryPolicy(**values["reentry.policy"])
        if asdict(policy) != values["reentry.policy"]:
            raise ValueError()
    except (TypeError, ValueError, InvalidOperation):
        raise ValueError("ENTRY_CONTROLS_INVALID_STRUCTURE_POLICY") from None
    from v2_core.trade_cycle import CyclePolicy

    try:
        if not isinstance(values["reentry.cycle_policy"], dict):
            raise TypeError()
        cycle = CyclePolicy(**values["reentry.cycle_policy"])
        if asdict(cycle) != values["reentry.cycle_policy"]:
            raise ValueError()
    except (TypeError, ValueError, InvalidOperation):
        raise ValueError("ENTRY_CONTROLS_INVALID_CYCLE_POLICY") from None
    if values["reentry.gate_scope"] == "TRADE_CYCLE":
        if set(values["reentry.enforce_sides"]) != {"BUY", "SELL"}:
            raise ValueError("ENTRY_CONTROLS_CYCLE_REQUIRES_BOTH_SIDES")
        needed = max(policy.anchor_bars, policy.atr_period + 1, policy.volume_period)
        if (needed + cycle.confirmation_bars + 2) * policy.timeframe_minutes > values[
            "reentry.max_minutes"
        ]:
            raise ValueError("ENTRY_CONTROLS_CYCLE_HISTORY_BUDGET")
    return values


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
    """Pure, account-bound verification for replay; never reads current config."""
    if not isinstance(saved, dict) or set(saved) != {"version", "values", "digest"}:
        raise ValueError("ENTRY_CONTROLS_PROOF_INVALID")
    version = saved["version"]
    if type(version) is not int or version < 0:
        raise ValueError("ENTRY_CONTROLS_PROOF_INVALID")
    values = resolve(saved["values"])
    # Old immutable proofs have no cycle fields. Verify their ORIGINAL digest,
    # never silently upgrade their policy or invalidate historical replay.
    if not (_CYCLE_FIELDS & saved["values"].keys()):
        values = {k: v for k, v in values.items() if k not in _CYCLE_FIELDS}
    account = asdict(scope) if hasattr(scope, "__dataclass_fields__") else scope
    if version and account.get("environment") != "SANDBOX":
        raise ValueError("ENTRY_CONTROLS_TESTNET_ONLY")
    fingerprint = digest(
        canonical({"scope": account, "version": version, "values": values})
    )
    if values != saved["values"] or fingerprint != saved["digest"]:
        raise ValueError("ENTRY_CONTROLS_PROOF_INVALID")
    return Controls(version, values, fingerprint)


def key(scope):
    return StateKey(**asdict(scope), namespace="entry-controls-v1", key="active")


def read(connect, scope):
    row = BusinessState(connect).read(key(scope))
    if row is not None and row.deleted:
        raise ValueError("ENTRY_CONTROLS_DELETED")
    values = resolve(json.loads(row.payload_json)["values"] if row else {})
    version = row.version if row else 0
    if version and scope.environment != "SANDBOX":
        raise ValueError("ENTRY_CONTROLS_TESTNET_ONLY")
    fingerprint = digest(
        canonical({"scope": asdict(scope), "version": version, "values": values})
    )
    return Controls(version, values, fingerprint)


def configure(connect, scope, changes, *, expected_version, reason):
    if scope.environment != "SANDBOX":
        raise ValueError("ENTRY_CONTROLS_TESTNET_ONLY")
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("ENTRY_CONTROLS_VERSION_REQUIRED")
    if (
        not isinstance(reason, str)
        or not 5 <= len(reason.strip()) <= 240
        or any(ord(c) < 32 for c in reason)
    ):
        raise ValueError("ENTRY_CONTROLS_REASON_REQUIRED")
    if not isinstance(changes, dict):
        raise TypeError("ENTRY_CONTROLS_UNKNOWN_FIELD")
    with connect() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + scope.key,),
        )
        connect_here = lambda: nullcontext(conn)
        old = read(connect_here, scope)
        if old.version != expected_version:
            raise ValueError("ENTRY_CONTROLS_VERSION_CONFLICT")
        values = resolve({**old.values, **changes})
        from v2_core.runtime_policy import PolicyStore

        policy = PolicyStore(connect_here, scope).read().values
        if not all(
            policy[k]
            for k in (
                "capital.enabled",
                "capital.model_enabled",
                "portfolio_risk.enabled",
            )
        ):
            raise ValueError("ENTRY_CONTROLS_REQUIRE_CAPITAL_GUARDS")
        changed = BusinessState(connect_here).change(
            key(scope),
            expected_version=expected_version,
            request_key=str(uuid4()),
            payload={"values": values, "reason": reason.strip()},
            reason="TESTNET_ENTRY_CONTROLS_CHANGED",
        )
        if changed.code != "APPLIED":
            raise ValueError("ENTRY_CONTROLS_VERSION_CONFLICT")
        return read(connect_here, scope)


def validate_submission(conn, scope, order_id, *, now_ms=None):
    """Called with the existing account submit lock; changed control requires a new proposal."""
    current = read(lambda: nullcontext(conn), scope)
    row = conn.execute(
        "SELECT e.snapshot#>'{features,context,entry_controls}' FROM v2_orders o "
        "JOIN v2_trade_intents i ON i.intent_id=o.episode_id "
        "JOIN v2_decision_evidence e USING(evidence_ref) WHERE o.order_id=%s",
        (order_id,),
    ).fetchone()
    if row is None:
        raise ValueError("ENTRY_CONTROLS_ORDER_MISSING")
    saved = row[0]
    if saved is None and current.version == 0:
        return
    if saved != current.snapshot():
        raise ValueError("ENTRY_CONTROLS_CHANGED_REPROPOSE")
    if (
        current.values["reentry.mode"] != "ENFORCE"
        or current.values["reentry.gate_scope"] != "TRADE_CYCLE"
    ):
        return None
    # Called under the existing account submit lock, OPEN only. No archive,
    # model call, sleep or new state write belongs in this final fence.
    from time import time_ns

    from v2_core.entry_history import prior_close
    from v2_core.entry_opportunities import validate

    now_ms = time_ns() // 1_000_000 if now_ms is None else now_ms
    row = conn.execute(
        "SELECT e.snapshot#>'{features,context,entry_structure}',"
        "i.payload->>'symbol',i.payload->>'side',e.snapshot->'observed_at' "
        "FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id "
        "JOIN v2_decision_evidence e USING(evidence_ref) WHERE o.order_id=%s "
        "AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)",
        (order_id, *asdict(scope).values()),
    ).fetchone()
    if row is None:
        raise ValueError("ENTRY_STRUCTURE_SUBMISSION_MISSING")
    proof, symbol, side, observed = row
    rejection = validate(
        proof,
        scope=asdict(scope),
        target=symbol,
        side=side,
        observed=observed,
        now=now_ms,
        expected_controls=current.snapshot(),
    )
    if rejection:
        raise ValueError(rejection)
    import psycopg

    # A timeout rolls back this savepoint, not the caller's transaction. The
    # account lock must never be held for a history catch-up query.
    try:
        with conn.transaction():
            old = conn.execute(
                "SELECT (SELECT setting::bigint FROM pg_settings WHERE name='statement_timeout'),"
                "(SELECT setting::bigint FROM pg_settings WHERE name='lock_timeout')"
            ).fetchone()
            limits = [
                min(value, bound) if value else bound
                for value, bound in zip(old, (500, 100), strict=True)
            ]
            conn.execute(
                "SELECT set_config('statement_timeout',%s,true),set_config('lock_timeout',%s,true)",
                tuple(f"{v}ms" for v in limits),
            )
            previous, missing = prior_close(conn, scope, symbol, now_ms)
            conn.execute(
                "SELECT set_config('statement_timeout',%s,true),set_config('lock_timeout',%s,true)",
                tuple(f"{v}ms" for v in old),
            )
    except psycopg.Error:
        raise ValueError("ENTRY_STRUCTURE_ANCHOR_UNAVAILABLE") from None
    if missing not in {None, "NO_PREVIOUS_FILLED_EPISODE"} or previous != proof.get(
        "previous_exit"
    ):
        raise ValueError("ENTRY_STRUCTURE_ANCHOR_CHANGED_REPROPOSE")
    return proof["valid_until_ms"]
