"""Versioned account-scoped business configuration; PostgreSQL is authoritative.

No credentials, executable imports or filesystem state. A decision keeps the
resolved policy snapshot; changing defaults never rewrites existing evidence.
"""

import json
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from v2_core.evidence import canonical, digest
from v2_core.state import BusinessState, StateKey

# Defaults preserve legacy behavior until an operator explicitly activates a
# profile. Bounds are validation invariants, not hidden trading thresholds.
# name: (default, minimum, maximum). Decimal settings are exact JSON strings.
SCHEMA = {
    "capital.enabled": (False, None, None),
    "capital.pool_fraction": ("0.70", "0.01", "1"),
    "capital.margin_buffer": ("1.10", "1", "2"),
    "capital.model_enabled": (False, None, None),
    "capital.initial_equity": ("1000", "1", "1000000000"),
    "capital.reference_wallet": ("0", "0", "1000000000"),
    "capital.started_at_ms": (0, 0, 4133980800000),
    "capital.profit_reinvest_fraction": ("0.50", "0", "1"),
    "capital.reduce_drawdown": ("0.03", "0.001", "0.5"),
    "capital.defensive_drawdown": ("0.06", "0.001", "0.5"),
    "capital.halt_drawdown": ("0.10", "0.001", "0.5"),
    "capital.reduced_factor": ("0.5", "0", "1"),
    "capital.defensive_factor": ("0.25", "0", "1"),
    "recovery.enabled": (False, None, None),
    "recovery.cooldown_ms": (7200000, 60000, 604800000),
    "recovery.max_attempts": (2, 1, 10),
    "recovery.probe_factor": ("0.20", "0.01", "0.25"),
    "recovery.loss_fraction": ("0.005", "0.0001", "0.02"),
    "recovery.max_losses": (2, 1, 10),
    "recovery.min_settlements": (3, 1, 100),
    "portfolio_risk.enabled": (False, None, None),
    "portfolio_risk.max_fraction": ("0.03", "0.001", "1"),
    "sizing.max_margin_fraction": ("1", "0.001", "1"),
    "sizing.cost_buffer_fraction": ("0", "0", "0.1"),
    "entry.force_isolated": (False, None, None),
    "entry.decision_lifetime_ms": (30000, 1000, 300000),
    "sizing.pool_fraction": ("0.80", "0.01", "1"),
    "sizing.min_allocation": ("0.03", "0", "1"),
    "sizing.max_allocation": ("0.15", "0.001", "1"),
    "sizing.risk_fraction": ("0.01", "0.00001", "1"),
    "sizing.atr_threshold": ("4", "0.01", "100"),
    "sizing.volatility_floor": ("0.2", "0", "1"),
    "entry.min_score": (30, 0, 100),
    "entry.short_min_strength": ("60", "0", "100"),
    "entry.takeover_chg_4h": ("3", "0", "1000"),
    "entry.takeover_chg_24h": ("10", "0", "1000"),
    "entry.extension_violent": ("1.25", "0", "100"),
    "entry.extension_default": ("2", "0", "100"),
    "entry.max_atr": ("6", "0.01", "100"),
    "entry.max_takeover_atr": ("12", "0.01", "100"),
    "entry.reversal_rsi_long": ("35", "0", "100"),
    "entry.reversal_rsi_short": ("65", "0", "100"),
    "entry.flow_long": ("0.52", "0", "1"),
    "entry.flow_short": ("0.48", "0", "1"),
    "entry.takeover_pullback_atr": ("1.5", "0", "100"),
    "entry.takeover_pullback_fraction": ("0.02", "0", "1"),
    "entry.pump_down_guard_chg": ("15", "0", "1000"),
    "entry.min_reward_risk": ("1", "0", "100"),
    "entry.max_adverse_funding": ("0.001", "0", "1"),
    "score.flow_bonus": ("5", "0", "100"),
    "score.ratio_high": ("0.60", "0", "1"),
    "score.ratio_low": ("0.40", "0", "1"),
    "score.crowding_bonus": ("8", "0", "100"),
    "score.crowding_penalty": ("5", "0", "100"),
    "score.atr_threshold": ("4", "0", "100"),
    "score.atr_penalty": ("3", "0", "100"),
    "score.max_atr_penalty": ("15", "0", "100"),
    "score.extension_penalty": ("4", "0", "100"),
    "score.max_extension_penalty": ("15", "0", "100"),
    "score.age_step_seconds": ("30", "1", "86400"),
    "score.max_age_penalty": ("10", "0", "100"),
    "leverage.low_score": (60, 0, 100),
    "leverage.adaptive_enabled": (False, None, None),
    "leverage.adaptive_high": (5, 2, 5),
    "leverage.boost_enabled": (False, None, None),
    "leverage.boost": (8, 2, 8),
    "leverage.boost_score": (90, 85, 100),
    "leverage.boost_max_atr": ("2", "0.1", "4"),
    "leverage.boost_max_age_ms": (30000, 1000, 60000),
    "leverage.boost_max_funding": ("0.0003", "0", "0.001"),
    "leverage.boost_max_stop": ("0.04", "0.001", "0.04"),
    "leverage.max_margin_consumption": ("0.60", "0.1", "0.7"),
    "leverage.max_spread": ("0.001", "0.00001", "0.005"),
    "leverage.depth_multiple": ("5", "1", "100"),
    "leverage.high_score": (85, 0, 100),
    "leverage.atr_threshold": ("4", "0", "100"),
    "leverage.low": (2, 1, 5),
    "leverage.medium": (3, 1, 5),
    "leverage.volatile": (3, 1, 5),
    "leverage.pulse": (5, 1, 5),
    "leverage.trend": (3, 1, 5),
    "leverage.pump": (2, 1, 5),
    "leverage.takeover": (2, 1, 5),
    "stop.takeover": ("0.10", "0.001", "0.12"),
    "stop.panic": ("0.035", "0.001", "0.12"),
    "stop.pulse": ("0.04", "0.001", "0.12"),
    "stop.default": ("0.08", "0.001", "0.12"),
    "stop.atr_multiplier": ("2", "0", "100"),
    "stop.max_takeover": ("0.12", "0.001", "0.12"),
    "stop.max_default": ("0.08", "0.001", "0.12"),
    "analysis.min_samples": (6, 0, 1000000),
    "analysis.min_win_rate": ("35", "0", "100"),
    "analysis.min_quality": ("40", "0", "100"),
    "analysis.min_follow_pct": ("-0.8", "-100", "100"),
    "analysis.soft_factor": ("0.5", "0", "1"),
    "scheduler.signal_batch": (20, 1, 1000),
    "scheduler.tv_first": (False, None, None),
    "scheduler.expiry_batch": (1000, 1, 5000),
    "scheduler.retry_seconds": (5, 1, 3600),
    "scheduler.lease_seconds": (300, 1, 3600),
    "scheduler.recovery_overdue_ms": (60000, 1000, 3600000),
    "health.order_seconds": (60, 5, 3600),
    "health.prepared_seconds": (90, 5, 3600),
    "health.settlement_seconds": (180, 10, 86400),
    "health.queue_seconds": (30, 5, 3600),
    "health.pipeline_seconds": (120, 5, 3600),
    "health.safety_seconds": (30, 5, 3600),
    "exit.adverse_funding": ("0.005", "0", "1"),
    "exit.emergency_loss_pct": ("5", "0.01", "100"),
    "exit.early_loss_minutes": (5, 0, 100000),
    "exit.early_loss_pct": ("2", "0.01", "100"),
    "exit.stagnation_minutes": (90, 1, 100000),
    "exit.stagnation_r": ("0.25", "0", "100"),
    "exit.reversal_minutes": (60, 0, 100000),
    "exit.reversal_max_return_pct": ("40", "0", "10000"),
    "exit.ema_reversal_fraction": ("0.02", "0", "1"),
}
for tag, values in {
    "S6A": (120, "5", "0.5", "3", "2", "2.5"),
    "S6B": (480, "8", "0.3", "5", "3", "5"),
    "S8": (240, "5", "0.3", "3", "2", "2"),
}.items():
    for name, value in zip(
        ("time_min", "partial_pct", "partial_ratio", "peak", "drawdown", "be"),
        values,
        strict=True,
    ):
        SCHEMA[f"exit.{tag}.{name}"] = (
            (value, 1, 100000)
            if isinstance(value, int)
            else (value, "0", "1" if name == "partial_ratio" else "10000")
        )


def resolve(values=None):
    supplied = {} if values is None else values
    if not isinstance(supplied, dict) or set(supplied) - SCHEMA.keys():
        raise ValueError("UNKNOWN_POLICY_SETTING")
    result = {}
    for name, (default, lower, upper) in SCHEMA.items():
        value = supplied.get(name, default)
        if type(value) is not type(default):
            raise ValueError("INVALID_POLICY_TYPE:" + name)
        if type(default) is bool:
            result[name] = value
            continue
        try:
            if isinstance(value, str) and len(value) > 80:
                raise ValueError("INVALID_POLICY_NUMBER:" + name)
            numeric = Decimal(value) if isinstance(value, str) else value
        except InvalidOperation:
            raise ValueError("INVALID_POLICY_NUMBER:" + name) from None
        if isinstance(numeric, Decimal) and not numeric.is_finite():
            raise ValueError("INVALID_POLICY_NUMBER:" + name)
        if not Decimal(str(lower)) <= numeric <= Decimal(str(upper)):
            raise ValueError("INVALID_POLICY_RANGE:" + name)
        result[name] = value
    if Decimal(result["sizing.min_allocation"]) > Decimal(
        result["sizing.max_allocation"]
    ):
        raise ValueError("INVERTED_ALLOCATION_RANGE")
    if result["leverage.low_score"] > result["leverage.high_score"]:
        raise ValueError("INVERTED_LEVERAGE_SCORE")
    if not (
        Decimal(result["capital.reduce_drawdown"])
        < Decimal(result["capital.defensive_drawdown"])
        < Decimal(result["capital.halt_drawdown"])
    ):
        raise ValueError("INVERTED_DRAWDOWN_THRESHOLDS")
    if result["capital.model_enabled"] and (
        not result["capital.enabled"]
        or Decimal(result["capital.reference_wallet"]) <= 0
    ):
        raise ValueError("CAPITAL_MODEL_REQUIRES_ANCHORED_BUDGET")
    if result["portfolio_risk.enabled"] and not result["capital.enabled"]:
        raise ValueError("PORTFOLIO_RISK_REQUIRES_CAPITAL_BUDGET")
    if result["recovery.enabled"] and not result["capital.model_enabled"]:
        raise ValueError("RECOVERY_REQUIRES_CAPITAL_MODEL")
    if result["leverage.boost_enabled"] and not result["leverage.adaptive_enabled"]:
        raise ValueError("BOOST_REQUIRES_ADAPTIVE_LEVERAGE")
    if result["leverage.boost"] not in {2, 3, 5, 8}:
        raise ValueError("UNSUPPORTED_BOOST_TIER")
    if result["leverage.adaptive_enabled"] and not (
        result["entry.force_isolated"]
        and result["portfolio_risk.enabled"]
        and result["leverage.low"]
        <= result["leverage.medium"]
        <= result["leverage.adaptive_high"]
    ):
        raise ValueError("ADAPTIVE_LEVERAGE_REQUIRES_ORDERED_ISOLATED_RISK_TIERS")
    if result["capital.enabled"] and Decimal(result["sizing.pool_fraction"]) != Decimal(
        result["capital.pool_fraction"]
    ):
        raise ValueError("SIZING_AND_CAPITAL_BUDGET_MISMATCH")
    return result


@dataclass(frozen=True)
class PolicySnapshot:
    version: int
    values_json: str

    @property
    def values(self):
        return json.loads(self.values_json)

    @property
    def digest(self):
        return digest(self.values_json)


class PolicyStore:
    def __init__(self, connect, scope):
        self.connect = connect
        self.store, self.scope = BusinessState(connect), scope
        self.key = StateKey(
            **asdict(scope), namespace="runtime-policy-v1", key="active"
        )

    def read(self):
        state = self.store.read(self.key)
        if state is None:
            return PolicySnapshot(0, canonical(resolve()))
        if state.deleted:
            raise ValueError("ACTIVE_POLICY_DELETED")
        return PolicySnapshot(
            state.version, canonical(resolve(json.loads(state.payload_json)["values"]))
        )

    def patch(self, changes, *, expected_version, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("CONFIGURATION_CHANGE_REASON_REQUIRED")
        before = self.read()
        if before.version != expected_version:
            raise ValueError("POLICY_VERSION_CONFLICT")
        if not isinstance(changes, dict) or set(changes) - SCHEMA.keys():
            raise ValueError("UNKNOWN_POLICY_SETTING")
        values = resolve({**before.values, **changes})
        if values["capital.enabled"] and self.scope.environment != "SANDBOX":
            raise ValueError("TESTNET_CAPITAL_POLICY_ONLY")
        with self.connect() as conn:
            # Do not change capital rules between a guarded account inspection
            # and its actual POST. No network I/O occurs in configuration writes.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("testnet-protocol-account:" + self.scope.key,),
            )
            result = BusinessState(lambda: nullcontext(conn)).change(
                self.key,
                expected_version=expected_version,
                request_key=str(uuid4()),
                payload={"values": values, "reason": reason},
                reason="RUNTIME_POLICY_ACTIVATED",
            )
        if result.code != "APPLIED":
            raise ValueError("POLICY_VERSION_CONFLICT")
        return PolicySnapshot(result.version, canonical(values))
