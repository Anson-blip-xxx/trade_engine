"""Frozen, exact capital projections for committed immutable strategy plans.

These readers authorize neither an entry nor a deployment. Historical plans
remain readable when entries have expired or the deployment has been disabled.
Batch readers do not fetch legacy market-history feature blobs or add N+1 I/O.
"""

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from fractions import Fraction
from uuid import UUID

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical
from v2_core.ledger import amount
from v2_core.strategy_plan import TradePlan
from v2_core.strategy_plan_store import read_trade_plan_record

CAPITAL_SCHEMA = "strategy-capital-v1"
CAPITAL_EVIDENCE_SCHEMA = "strategy-capital-evidence-v1"

# The aliases are deliberately fixed so portfolio/reservation queries can append
# this one column to their existing intent/evidence joins. A present JSON null
# plan stays a record and is rejected; it must never become the legacy NULL arm.
STRATEGY_CAPITAL_RECORD_SQL = """CASE WHEN e.snapshot ? 'strategy_plan' THEN
jsonb_build_object(
    'scope', jsonb_build_object('exchange',i.exchange,'account_id',i.account_id,
        'environment',i.environment,'product',i.product),
    'producer',i.producer,'strategy_version',i.strategy_version,
    'config_digest',i.config_digest,'payload',i.payload,
    'evidence_strategy_version',e.strategy_version,
    'evidence_config_digest',e.config_digest,'config',e.config,
    'snapshot',jsonb_build_object(
        'strategy_plan',e.snapshot->'strategy_plan',
        'observed_at',e.snapshot->'observed_at',
        'decided_at',e.snapshot->'decided_at',
        'expires_at_ms',e.snapshot->'expires_at_ms',
        'symbol',e.snapshot->'symbol',
        'features',CASE WHEN jsonb_typeof(e.snapshot->'features')='object' THEN
            CASE WHEN (e.snapshot->'features') ? 'capital' THEN
                jsonb_build_object('capital',e.snapshot->'features'->'capital')
            ELSE '{}'::jsonb END
        ELSE e.snapshot->'features' END))
ELSE NULL END"""


def _decimal(value, *, derived=False):
    if isinstance(value, Decimal):
        value = str(value)
    if derived:
        # Products of two validated NUMERIC(38,18) facts can have scale 36.
        # Preserve those exact derived digits until the final risk/margin ceil;
        # applying the storage-fact scale here would reject valid small lots.
        if type(value) is not str or len(value) > 200:
            raise ValueError("STRATEGY_CAPITAL_DERIVED_VALUE_INVALID")
        try:
            number = Decimal(value)
            if (
                not number.is_finite()
                or number.copy_abs() >= Decimal("1e20")
                or number.as_tuple().exponent < -72
            ):
                raise ValueError("STRATEGY_CAPITAL_DERIVED_VALUE_INVALID")
            return number
        except ArithmeticError:
            raise ValueError("STRATEGY_CAPITAL_DERIVED_VALUE_INVALID") from None
    return amount(value)


def _ceil(value):
    """Ceil an exact rational to NUMERIC(38,18), without Decimal context math."""
    scale = 10**18
    units = -(-(value.numerator * scale) // value.denominator)
    integer, fraction = divmod(units, scale)
    return amount(f"{integer}.{fraction:018d}")


def _fraction(value, *, maximum, reason):
    try:
        # Serialized policy boundaries use decimal strings, never floats/ints.
        if type(value) is not str:
            raise ValueError(reason)
        result = amount(value, positive=True)
        if not Decimal("0.0001") <= result <= Decimal(maximum):
            raise ValueError(reason)
        return result
    except (ValueError, TypeError):
        raise ValueError(reason) from None


@dataclass(frozen=True)
class StrategyCapitalPlan:
    plan: TradePlan
    cost_fraction: Decimal
    price_drift_fraction: Decimal
    _leverage_proof_json: str | None = None

    def __post_init__(self):
        if type(self.plan) is not TradePlan:
            raise ValueError("STRATEGY_CAPITAL_PLAN_REQUIRED")
        # Revalidate and detach the plan as well as optional nested proof data.
        object.__setattr__(self, "plan", TradePlan.from_payload(self.plan.payload()))
        for name, maximum in (
            ("cost_fraction", "0.1"),
            ("price_drift_fraction", "0.05"),
        ):
            if type(getattr(self, name)) is not Decimal:
                raise ValueError("STRATEGY_CAPITAL_CONFIG_INVALID")
            object.__setattr__(
                self,
                name,
                _fraction(
                    str(getattr(self, name)),
                    maximum=maximum,
                    reason="STRATEGY_CAPITAL_CONFIG_INVALID",
                ),
            )
        if self._leverage_proof_json is not None:
            try:
                proof = json.loads(self._leverage_proof_json)
                _validate_leverage(proof, self.plan)
                object.__setattr__(self, "_leverage_proof_json", canonical(proof))
            except (ValueError, TypeError, RecursionError):
                raise ValueError("STRATEGY_CAPITAL_EVIDENCE_INVALID") from None

    @property
    def leverage_proof(self):
        return (
            json.loads(self._leverage_proof_json)
            if self._leverage_proof_json is not None
            else None
        )

    @property
    def stop_fraction(self):
        entry = Fraction(amount(self.plan.entry_price, positive=True))
        stop = Fraction(amount(self.plan.stop_price, positive=True))
        return _ceil(abs(entry - stop) / entry)

    def stop_risk(self, notional, *, current_cost_fraction):
        """Retain original full-plan risk; never release it after price crosses SL.

        The immutable initial adverse-price envelope and frozen cost floor are
        compared with the current full-plan notional. Current policy may increase
        cost but cannot silently reduce the originally admitted risk budget.
        """
        try:
            current_notional = Fraction(_decimal(notional, derived=True))
            current_cost = Fraction(_decimal(current_cost_fraction))
            if current_notional < 0 or current_cost < 0:
                raise ValueError("negative capital input")
        except (ValueError, TypeError):
            raise ValueError("STRATEGY_CAPITAL_RISK_INPUT_INVALID") from None
        quantity = Fraction(amount(self.plan.quantity, positive=True))
        entry = Fraction(amount(self.plan.entry_price, positive=True))
        stop = Fraction(amount(self.plan.stop_price, positive=True))
        drift = Fraction(self.price_drift_fraction)
        cost = max(Fraction(self.cost_fraction), current_cost)
        original = entry * (1 + drift if self.plan.side == "BUY" else 1 - drift)

        def price_risk(price):
            distance = price - stop if self.plan.side == "BUY" else stop - price
            return max(Fraction(0), distance) * quantity + price * quantity * cost

        result = max(
            Fraction(amount(self.plan.planned_loss, positive=True)),
            price_risk(original),
            price_risk(current_notional / quantity),
        )
        try:
            return _ceil(result)
        except ValueError:
            raise ValueError("STRATEGY_CAPITAL_RISK_OUT_OF_RANGE") from None


def _validate_leverage(proof, plan):
    """Reject malformed normalized venue facts before any leverage arithmetic.

    This validates evidence, not leverage safety. The current admission policy
    still checks selected tiers, liquidity, maintenance consumption and boosts.
    No arbitrary age limit is invented: observations must belong to the plan's
    frozen observation-to-decision window, never its future.
    """
    try:
        if (
            type(proof) is not dict
            or type(proof.get("selected")) is not int
            or proof["selected"] != plan.leverage
            or type(proof.get("venue")) is not dict
            or type(proof.get("checks")) is not dict
            or any(type(value) is not bool for value in proof["checks"].values())
        ):
            raise ValueError
        venue = proof["venue"]
        if venue.get("symbol") != plan.symbol:
            raise ValueError
        for name in ("observed_at_ms", "book_at_ms"):
            if (
                type(venue.get(name)) is not int
                or not plan.observed_at_ms <= venue[name] <= plan.decided_at_ms
            ):
                raise ValueError
        if (
            _decimal(venue["spread_fraction"], derived=True) < 0
            or _decimal(venue["top_notional"], derived=True) <= 0
        ):
            raise ValueError
        tiers = venue["tiers"]
        if type(tiers) is not list or not tiers:
            raise ValueError
        previous_cap = Decimal(0)
        for tier in tiers:
            if type(tier) is not dict:
                raise ValueError
            floor, cap, ratio = (amount(tier[name]) for name in ("floor", "cap", "mmr"))
            if (
                floor != previous_cap
                or not 0 <= floor < cap
                or not 0 < ratio < 1
                or type(tier["max_leverage"]) is not int
                or not 1 <= tier["max_leverage"] <= 125
            ):
                raise ValueError
            previous_cap = cap
        canonical(proof)
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
        raise ValueError("STRATEGY_CAPITAL_EVIDENCE_INVALID") from None


def read_strategy_capital(record, *, scope):
    """Read one scoped SQL projection; only the legacy NULL arm returns None."""
    if type(scope) is not AccountScope:
        raise ValueError("STRATEGY_CAPITAL_SCOPE_REQUIRED")
    if record is None:
        return None
    if (
        type(record) is not dict
        or type(record.get("snapshot")) is not dict
        or "strategy_plan" not in record["snapshot"]
    ):
        raise ValueError("STRATEGY_CAPITAL_RECORD_INVALID")
    plan = read_trade_plan_record(record)
    if plan.scope != scope:
        raise ValueError("STRATEGY_CAPITAL_SCOPE_MISMATCH")
    config = record["config"]
    if "capital" not in config:
        raise ValueError("STRATEGY_CAPITAL_CONFIG_REQUIRED")
    capital = config["capital"]
    if (
        type(capital) is not dict
        or set(capital) != {"schema", "roundtrip_cost_fraction", "price_drift_fraction"}
        or capital.get("schema") != CAPITAL_SCHEMA
    ):
        raise ValueError("STRATEGY_CAPITAL_CONFIG_INVALID")
    cost = _fraction(
        capital["roundtrip_cost_fraction"],
        maximum="0.1",
        reason="STRATEGY_CAPITAL_CONFIG_INVALID",
    )
    drift = _fraction(
        capital["price_drift_fraction"],
        maximum="0.05",
        reason="STRATEGY_CAPITAL_CONFIG_INVALID",
    )
    features = record["snapshot"].get("features")
    if type(features) is not dict:
        raise ValueError("STRATEGY_CAPITAL_EVIDENCE_INVALID")
    proof_json = None
    if "capital" in features:
        evidence = features["capital"]
        if (
            type(evidence) is not dict
            or set(evidence) != {"schema", "leverage_decision"}
            or evidence.get("schema") != CAPITAL_EVIDENCE_SCHEMA
        ):
            raise ValueError("STRATEGY_CAPITAL_EVIDENCE_INVALID")
        try:
            _validate_leverage(evidence["leverage_decision"], plan)
            proof_json = canonical(evidence["leverage_decision"])
        except (ValueError, TypeError, RecursionError):
            raise ValueError("STRATEGY_CAPITAL_EVIDENCE_INVALID") from None
    return StrategyCapitalPlan(plan, cost, drift, proof_json)


def read_strategy_capital_for_episode(conn, episode, *, scope):
    if type(scope) is not AccountScope:
        raise ValueError("STRATEGY_CAPITAL_SCOPE_REQUIRED")
    row = conn.execute(
        "SELECT "
        + STRATEGY_CAPITAL_RECORD_SQL
        + " FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref) "
        "WHERE i.intent_id=%s "
        "AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)",
        (str(UUID(str(episode))), *asdict(scope).values()),
    ).fetchone()
    if row is None:
        raise ValueError("STRATEGY_PLAN_INTENT_REQUIRED")
    return read_strategy_capital(row[0], scope=scope)
