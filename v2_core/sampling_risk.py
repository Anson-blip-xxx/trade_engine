"""Pure proof for the reviewed SANDBOX sampling ceilings.

This profile changes new-order risk budgets only. Existing reservations keep
their recorded risk; no capital, settlement, stop or exit state is rewritten.
"""

from dataclasses import asdict
from decimal import Decimal

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.runtime_policy import PolicySnapshot, resolve


def build_sampling_risk_proof(snapshot, scope):
    """Validate prerequisites and return an exact, reproducible policy diff."""
    if not isinstance(scope, AccountScope) or (
        scope.exchange,
        scope.environment,
        scope.product,
    ) != ("BINANCE", "SANDBOX", "FUTURES"):
        raise ValueError("SAMPLING_RISK_SANDBOX_FUTURES_ONLY")
    if not isinstance(snapshot, PolicySnapshot):
        raise TypeError("SAMPLING_RISK_POLICY_SNAPSHOT_REQUIRED")
    if type(snapshot.version) is not int or snapshot.version < 1:
        raise ValueError("SAMPLING_RISK_EXISTING_POLICY_REQUIRED")
    before = resolve(snapshot.values)
    if not before["capital.enabled"] or not before["portfolio_risk.enabled"]:
        raise ValueError("SAMPLING_RISK_CAPITAL_GUARDS_REQUIRED")
    if not before["capital.model_enabled"]:
        raise ValueError("SAMPLING_RISK_CAPITAL_MODEL_REQUIRED")
    if before["settlement.cash_basis_version"] != 2:
        raise ValueError("SAMPLING_RISK_CASH_BASIS_2_REQUIRED")
    if not (
        Decimal(before["capital.pool_fraction"])
        == Decimal(before["sizing.pool_fraction"])
        <= Decimal("0.70")
    ):
        raise ValueError("SAMPLING_RISK_POOL_CAP_REQUIRED")
    targets = {
        "sizing.risk_fraction": "0.0025",
        "portfolio_risk.max_fraction": "0.01",
    }
    if any(Decimal(before[key]) < Decimal(value) for key, value in targets.items()):
        raise ValueError("SAMPLING_RISK_MUST_NOT_INCREASE")
    changes = {key: value for key, value in targets.items() if before[key] != value}
    after = resolve({**before, **changes})
    preserved = {key: value for key, value in before.items() if key not in targets}
    if preserved != {key: value for key, value in after.items() if key not in targets}:
        raise ValueError("SAMPLING_RISK_UNRELATED_POLICY_CHANGE")
    return {
        "profile": "SANDBOX_LOW_RISK_SAMPLING_V1",
        "scope": asdict(scope),
        "before_version": snapshot.version,
        "before_digest": snapshot.digest,
        "after_digest": PolicySnapshot(snapshot.version, canonical(after)).digest,
        "changes": changes,
        "diff": {
            key: {"before": before[key], "after": value}
            for key, value in changes.items()
        },
        "preserved_policy_digest": digest(canonical(preserved)),
        "already_applied": not changes,
        "existing_reservations": "PRESERVED_ORIGINAL_RISK",
        "new_order_remaining_risk": "MAX_ZERO_NEW_LIMIT_MINUS_HELD_RISK",
    }
