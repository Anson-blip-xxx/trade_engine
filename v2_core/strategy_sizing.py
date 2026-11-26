"""Pure typed sizing from explicit, scoped facts; never execution permission.

The caller must obtain authentic account, reservation, venue and leverage facts.
This module validates their binding and causal age, not their external truth.
Admission must re-read capital and reservations under its account/route fence.
No registry lookup, database, venue call, legacy score or model approval occurs.

``used_margin`` includes venue position AND open-order initial margin.
``pending_reserved_margin`` covers HELD margin not yet represented at the venue;
conservative double counting is safe, omitting pending reservations is not.
``remaining_stop_risk`` is the already drawdown-adjusted portfolio residual.
The provider sets ``quote_at_ms`` to the oldest quote/mark observation, and
``account_at_ms`` to the oldest fact in the consistent capital/reservation
snapshot. Collection completion does not make older components fresh again.
An explicit provider-selected leverage is checked, never inferred from a fake
directional score. Even a verified 8x proof is not a liquidation guarantee.
"""

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from fractions import Fraction

from v2_core.account_risk import AccountScope
from v2_core.adaptive_leverage import BOOST_CHECKS
from v2_core.evidence import canonical
from v2_core.ingress import milliseconds, symbol
from v2_core.precision import amount, decimal_text, money_context
from v2_core.sizing_budget import price_envelope
from v2_core.strategy_candidate_builder import StructureCandidateDecision
from v2_core.strategy_capital import StrategyCapitalPlan
from v2_core.strategy_plan import PLAN_SCHEMA, TradePlan
from v2_core.strategy_registry import DeploymentBinding

SIZING_VERSION = "strategy-sizing-v1"
_SCALE = 10**18


def _object(encoded):
    if type(encoded) is not str or len(encoded.encode("utf-8")) > 65536:
        raise ValueError("SIZING_JSON_BOUND")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("SIZING_DUPLICATE_JSON_FIELD")
            result[key] = value
        return result

    parsed = json.loads(encoded, object_pairs_hook=unique)
    if type(parsed) is not dict:
        raise ValueError("SIZING_JSON_OBJECT_REQUIRED")
    normalized = canonical(parsed)
    if len(normalized.encode("utf-8")) > 65536:
        raise ValueError("SIZING_JSON_BOUND")
    return normalized


def _fraction(value, *, positive=False):
    return Fraction(amount(value, positive=positive))


def _text(value, *, up=False):
    """Round derived nonnegative values explicitly at the storage boundary."""
    value = Fraction(value)
    if value < 0:
        raise ValueError("SIZING_NEGATIVE_DERIVED_VALUE")
    scaled = value * _SCALE
    units = (
        -(-scaled.numerator // scaled.denominator)
        if up
        else scaled.numerator // scaled.denominator
    )
    # Decimal construction/scaleb can depend on the caller's context. String
    # formatting from integers keeps the boundary exact even with traps set.
    whole, remainder = divmod(units, _SCALE)
    result = f"{whole}.{remainder:018d}".rstrip("0").rstrip(".")
    return decimal_text(amount(result))


@dataclass(frozen=True)
class StrategySizingPolicy:
    pool_fraction: str
    single_margin_fraction: str
    single_risk_fraction: str
    cost_fraction: str
    price_drift_fraction: str
    stop_atr_multiple: str
    structure_buffer_atr: str
    max_stop_fraction: str
    max_margin_consumption: str
    max_spread_fraction: str
    depth_multiple: str
    allowed_leverages: tuple[int, ...]
    boost_enabled: bool
    max_fact_age_ms: int
    max_quote_age_ms: int
    entry_ttl_ms: int

    def __post_init__(self):
        bounds = {
            "pool_fraction": ("0", ".70", False),
            "single_margin_fraction": ("0", ".70", False),
            "single_risk_fraction": ("0", "1", False),
            "cost_fraction": (".0001", ".1", True),
            "price_drift_fraction": (".0001", ".05", True),
            "stop_atr_multiple": ("0", "100", False),
            "structure_buffer_atr": ("0", "100", True),
            "max_stop_fraction": (".0001", ".5", True),
            "max_margin_consumption": ("0", "1", False),
            "max_spread_fraction": ("0", ".05", True),
            "depth_multiple": ("1", "1000000", True),
        }
        for name, (low, high, inclusive) in bounds.items():
            value = amount(getattr(self, name))
            if value > Decimal(high) or (
                value < Decimal(low) if inclusive else value <= Decimal(low)
            ):
                raise ValueError("SIZING_POLICY_RANGE")
            object.__setattr__(self, name, decimal_text(value))
        if amount(self.single_margin_fraction) > amount(self.pool_fraction):
            raise ValueError("SIZING_SINGLE_MARGIN_EXCEEDS_POOL")
        if (
            type(self.allowed_leverages) is not tuple
            or not self.allowed_leverages
            or any(
                type(x) is not int or x not in {2, 3, 5, 8}
                for x in self.allowed_leverages
            )
            or self.allowed_leverages != tuple(sorted(set(self.allowed_leverages)))
            or type(self.boost_enabled) is not bool
        ):
            raise ValueError("SIZING_EXPLICIT_LEVERAGE_POLICY_REQUIRED")
        for name in ("max_fact_age_ms", "max_quote_age_ms", "entry_ttl_ms"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 86400000:
                raise ValueError("SIZING_POLICY_TIME_RANGE")
        if self.entry_ttl_ms == 0:
            raise ValueError("SIZING_POLICY_TIME_RANGE")

    def payload(self):
        value = asdict(self)
        value["allowed_leverages"] = list(self.allowed_leverages)
        return value

    def binding_config(self):
        return {"schema": SIZING_VERSION, "policy": self.payload()}

    def capital_config(self):
        return {
            "schema": "strategy-capital-v1",
            "roundtrip_cost_fraction": self.cost_fraction,
            "price_drift_fraction": self.price_drift_fraction,
        }


@dataclass(frozen=True)
class StrategySizingFacts:
    scope: AccountScope
    symbol: str
    observed_at_ms: int
    available_at_ms: int
    quote_at_ms: int
    account_at_ms: int
    filters_at_ms: int
    entry_price: str
    mark_price: str
    quantity_step: str
    min_quantity: str
    max_quantity: str
    min_notional: str
    max_notional: str
    price_tick: str
    min_price: str
    max_price: str
    capital_base: str
    available_margin: str
    used_margin: str
    pending_reserved_margin: str
    remaining_stop_risk: str
    drawdown_factor: str
    selected_leverage: int
    leverage_proof_json: str

    def __post_init__(self):
        if type(self.scope) is not AccountScope:
            raise TypeError("SIZING_ACCOUNT_SCOPE_REQUIRED")
        object.__setattr__(self, "scope", AccountScope(**asdict(self.scope)))
        if (self.scope.exchange, self.scope.product) != ("BINANCE", "FUTURES"):
            raise ValueError("SIZING_MARKET_UNSUPPORTED")
        symbol(self.symbol)
        for name in (
            "observed_at_ms",
            "available_at_ms",
            "quote_at_ms",
            "account_at_ms",
            "filters_at_ms",
        ):
            milliseconds(getattr(self, name))
        if not (
            max(self.quote_at_ms, self.account_at_ms, self.filters_at_ms)
            <= self.observed_at_ms
            <= self.available_at_ms
        ):
            raise ValueError("SIZING_NONCAUSAL_FACTS")
        positive = (
            "entry_price",
            "mark_price",
            "quantity_step",
            "min_quantity",
            "max_quantity",
            "min_notional",
            "max_notional",
            "price_tick",
            "min_price",
            "max_price",
        )
        nonnegative = (
            "capital_base",
            "available_margin",
            "used_margin",
            "pending_reserved_margin",
            "remaining_stop_risk",
            "drawdown_factor",
        )
        for name in positive + nonnegative:
            value = amount(getattr(self, name), positive=name in positive)
            if value < 0:
                raise ValueError("SIZING_NEGATIVE_FACT")
            object.__setattr__(self, name, decimal_text(value))
        if amount(self.drawdown_factor) > 1:
            raise ValueError("SIZING_DRAWDOWN_FACTOR_RANGE")
        for lower, upper in (
            ("min_quantity", "max_quantity"),
            ("min_notional", "max_notional"),
            ("min_price", "max_price"),
        ):
            if amount(getattr(self, lower)) > amount(getattr(self, upper)):
                raise ValueError("SIZING_INVERTED_VENUE_LIMITS")
        if type(self.selected_leverage) is not int or self.selected_leverage not in {
            2,
            3,
            5,
            8,
        }:
            raise ValueError("SIZING_LEVERAGE_UNSUPPORTED")
        object.__setattr__(
            self, "leverage_proof_json", _object(self.leverage_proof_json)
        )

    @property
    def leverage_proof(self):
        return json.loads(self.leverage_proof_json)

    def payload(self):
        result = asdict(self)
        result.pop("leverage_proof_json")
        result["leverage_proof"] = self.leverage_proof
        return result


@dataclass(frozen=True)
class StrategySizingResult:
    status: str
    reason: str
    plan: TradePlan | None
    evidence_json: str

    def __post_init__(self):
        if self.status not in {"SIZED", "NO_BUDGET"} or not (
            (self.status == "SIZED") == (type(self.plan) is TradePlan)
        ):
            raise ValueError("SIZING_RESULT_INVALID")
        if self.plan is not None:
            object.__setattr__(
                self, "plan", TradePlan.from_payload(self.plan.payload())
            )
        object.__setattr__(self, "evidence_json", _object(self.evidence_json))

    @property
    def evidence(self):
        return json.loads(self.evidence_json)

    @property
    def capital_evidence(self):
        return self.evidence["capital"]

    @property
    def execution_authorized(self):
        return False


def _safe_margin(leverage, stop, notional, venue, policy):
    """Exact rational equivalent of adaptive_leverage.safe_margin, without score.

    The existing check uses Decimal division. A sizing boundary must remain
    conservative even when ambient precision/traps are hostile; tier selection
    and the final inequality are exact here, with the same worst-MMR convention.
    """
    cost = _fraction(policy.cost_fraction)
    stress = notional * (1 + stop + cost)
    tiers = venue["tiers"]
    if not all(
        any(
            Fraction(row["floor"]) <= value < Fraction(row["cap"])
            and leverage <= row["max_leverage"]
            for row in tiers
        )
        for value in (notional, stress)
    ):
        return False
    maintenance = max(
        Fraction(row["mmr"]) for row in tiers if Fraction(row["floor"]) <= stress
    )
    return (stop + cost + stress / notional * maintenance) * leverage <= _fraction(
        policy.max_margin_consumption
    )


def size_candidate(
    decision, *, binding, policy, facts, expires_at_ms, exit_version, exit_config
):
    """Build a typed plan, or a bounded non-terminal NO_BUDGET observation.

    No first-writer candidate is overwritten, and no signal is terminalized here.
    Caller recovery must precede sizing. ``expires_at_ms`` must additionally fit
    the original signal at the frozen-candidate assembly boundary.
    """
    if (
        type(decision) is not StructureCandidateDecision
        or type(binding) is not DeploymentBinding
        or type(policy) is not StrategySizingPolicy
        or type(facts) is not StrategySizingFacts
    ):
        raise TypeError("SIZING_TYPED_INPUTS_REQUIRED")
    verified = StructureCandidateDecision(
        decision.structure_json,
        decision.policy,
        decision.available_at_ms,
        decision.decided_at_ms,
    )
    if verified.payload() != decision.payload():
        raise ValueError("SIZING_CANDIDATE_MISMATCH")
    policy = StrategySizingPolicy(**asdict(policy))
    facts = StrategySizingFacts(**asdict(facts) | {"scope": facts.scope})
    binding = DeploymentBinding(
        binding.scope,
        binding.instance_id,
        binding.spec,
        binding.config_json,
        binding.generation,
        binding.mode,
    )
    structure = decision.structure
    if (
        facts.scope != binding.scope
        or facts.scope.environment != structure["environment"]
        or facts.symbol != structure["symbol"]
        or binding.spec.plan_schema != PLAN_SCHEMA
    ):
        raise ValueError("SIZING_SCOPE_OR_SCHEMA_MISMATCH")
    config = binding.config
    if (
        config.get("candidate_builder") != decision.policy.binding_config()
        or config.get("sizing") != policy.binding_config()
        or config.get("capital") != policy.capital_config()
    ):
        raise ValueError("SIZING_FROZEN_POLICY_MISMATCH")
    now = decision.decided_at_ms
    if facts.available_at_ms > now:
        raise ValueError("SIZING_FUTURE_FACTS")
    for stamp, limit in (
        (facts.observed_at_ms, policy.max_fact_age_ms),
        (facts.account_at_ms, policy.max_fact_age_ms),
        (facts.filters_at_ms, policy.max_fact_age_ms),
        (facts.quote_at_ms, policy.max_quote_age_ms),
    ):
        if now - stamp > limit:
            raise ValueError("SIZING_STALE_FACTS")
    milliseconds(expires_at_ms)
    if (
        not now
        < expires_at_ms
        <= min(now + policy.entry_ttl_ms, decision.observed_at_ms + 86400000)
    ):
        raise ValueError("SIZING_PLAN_LIFETIME")
    if type(exit_config) is not dict:
        raise TypeError("SIZING_EXIT_CONFIG_REQUIRED")
    exit_json = _object(canonical(exit_config))
    evidence = {
        "schema": SIZING_VERSION,
        "candidate_digest": decision.decision_digest,
        "policy": policy.payload(),
        "facts": facts.payload(),
        "capital": {
            "schema": "strategy-capital-evidence-v1",
            "leverage_decision": facts.leverage_proof,
        },
        "execution_authorized": False,
    }

    def result(reason, plan=None, **calculation):
        status = "SIZED" if plan is not None else "NO_BUDGET"
        return StrategySizingResult(
            status,
            reason,
            plan,
            canonical(
                {
                    **evidence,
                    "status": status,
                    "reason": reason,
                    "calculation": calculation,
                }
            ),
        )

    if decision.status != "CANDIDATE":
        return result(decision.status)
    if binding.mode not in {"ACTIVE", "SHADOW"}:
        return result("DEPLOYMENT_NOT_EVALUABLE")
    leverage = facts.selected_leverage
    proof = facts.leverage_proof
    if leverage not in policy.allowed_leverages or proof.get("selected") != leverage:
        raise ValueError("SIZING_LEVERAGE_POLICY_MISMATCH")
    if type(proof.get("checks")) is not dict or type(proof.get("venue")) is not dict:
        raise ValueError("SIZING_LEVERAGE_EVIDENCE_REQUIRED")
    if leverage == 8 and (
        not policy.boost_enabled
        or not all(proof.get("checks", {}).get(name) is True for name in BOOST_CHECKS)
    ):
        raise ValueError("SIZING_BOOST_UNVERIFIED")
    numbers = {
        name: _fraction(getattr(facts, name))
        for name in (
            "entry_price",
            "mark_price",
            "quantity_step",
            "price_tick",
            "min_price",
            "max_price",
            "capital_base",
            "available_margin",
            "used_margin",
            "pending_reserved_margin",
            "remaining_stop_risk",
            "drawdown_factor",
        )
    }
    price, mark = numbers["entry_price"], numbers["mark_price"]
    values = structure["values"]
    reference = _fraction(values["close"], positive=True)
    drift = _fraction(policy.price_drift_fraction)
    if abs(price - reference) > reference * drift or abs(mark - price) > price * drift:
        return result("REFERENCE_PRICE_DRIFT")
    atr = _fraction(values["atr"], positive=True)
    buffer = atr * _fraction(policy.structure_buffer_atr)
    distance = atr * _fraction(policy.stop_atr_multiple)
    side = decision.side
    raw_stop = (
        min(price - distance, _fraction(values["prior_swing_low"]) - buffer)
        if side == "BUY"
        else max(price + distance, _fraction(values["prior_swing_high"]) + buffer)
    )
    tick = numbers["price_tick"]
    units = raw_stop / tick
    stop = (
        units.numerator // units.denominator
        if side == "BUY"
        else -(-units.numerator // units.denominator)
    ) * tick
    if stop <= 0 or (stop >= price if side == "BUY" else stop <= price):
        return result("INVALID_STOP_TICK")
    if any(
        not numbers["min_price"] <= value <= numbers["max_price"]
        for value in (price, mark, stop)
    ):
        return result("VENUE_PRICE_LIMIT")
    stop_fraction = abs(price - stop) / price
    if stop_fraction > _fraction(policy.max_stop_fraction):
        return result("STOP_DISTANCE_LIMIT")
    # Reuse the existing fixed-stop envelope; a rounded-up stop fraction makes
    # it conservative, including fractions not representable in NUMERIC(38,18).
    with money_context():
        envelope = price_envelope(
            price=facts.entry_price,
            mark_price=facts.mark_price,
            drift_fraction=policy.price_drift_fraction,
            side="LONG" if side == "BUY" else "SHORT",
            stop_price=_text(stop),
            stop_fraction=_text(stop_fraction, up=True),
            cost_fraction=policy.cost_fraction,
        )
    if not envelope["stop_valid_through_envelope"]:
        return result("PRICE_ENVELOPE_CROSSES_STOP")
    high, low, unit_risk = (
        Fraction(envelope[name])
        for name in ("price_high", "price_low", "unit_stop_risk")
    )
    base, factor = numbers["capital_base"], numbers["drawdown_factor"]
    pending = numbers["pending_reserved_margin"]
    # Stored reserves round upward; the corresponding allocation caps round
    # downward so an extra storage quantum cannot breach the 70% reserve floor.
    pool = _fraction(
        _text(
            max(
                Fraction(0),
                base * _fraction(policy.pool_fraction)
                - numbers["used_margin"]
                - pending,
            )
        )
    )
    available = _fraction(
        _text(max(Fraction(0), numbers["available_margin"] - pending))
    )
    single_margin = _fraction(
        _text(base * _fraction(policy.single_margin_fraction) * factor)
    )
    # Round budgets DOWN before quantity sizing, then loss UP. This prevents a
    # ceil-to-storage loss from overshooting a fractional remaining risk cap.
    risk = _fraction(
        _text(
            min(
                base * _fraction(policy.single_risk_fraction) * factor,
                numbers["remaining_stop_risk"],
            )
        )
    )
    caps = {
        "POOL_MARGIN": pool * leverage / high,
        "AVAILABLE_MARGIN_AND_COST": available
        / (high / leverage + high * _fraction(policy.cost_fraction)),
        "SINGLE_MARGIN": single_margin * leverage / high,
        "STOP_RISK": risk / unit_risk,
        "VENUE_QUANTITY": _fraction(facts.max_quantity),
        "VENUE_NOTIONAL": _fraction(facts.max_notional) / high,
    }
    cap = min(caps.values())
    step = numbers["quantity_step"]
    quotient = cap / step
    quantity = (quotient.numerator // quotient.denominator) * step
    summary = {
        "pool_remaining": _text(pool),
        "available_after_pending": _text(available),
        "single_margin_limit": _text(single_margin),
        "stop_risk_limit": _text(risk),
        "binding_caps": sorted(name for name, value in caps.items() if value == cap),
        "raw_stop": str(raw_stop),
        "stop_price": _text(stop),
        "price_high": str(high),
        "price_low": str(low),
        "unit_stop_risk": str(unit_risk),
    }
    if quantity <= 0:
        return result("NO_USABLE_BUDGET", **summary)
    if quantity < _fraction(facts.min_quantity) or quantity * low < _fraction(
        facts.min_notional
    ):
        return result("BELOW_VENUE_MINIMUM", **summary)
    loss = _text(quantity * unit_risk, up=True)
    plan = TradePlan(
        scope=binding.scope,
        instance_id=binding.instance_id,
        strategy_id=binding.spec.strategy_id,
        code_version=binding.spec.code_version,
        deployment_generation=binding.generation,
        config_digest=binding.config_digest,
        opportunity_id=decision.opportunity_id,
        symbol=facts.symbol,
        side=side,
        quantity=_text(quantity),
        entry_price=facts.entry_price,
        stop_price=_text(stop),
        planned_loss=loss,
        quantity_step=facts.quantity_step,
        leverage=leverage,
        margin_mode="ISOLATED",
        observed_at_ms=decision.observed_at_ms,
        decided_at_ms=now,
        expires_at_ms=expires_at_ms,
        exit_handler=binding.spec.exit_handler,
        exit_version=exit_version,
        exit_config_json=exit_json,
    )
    strategy = StrategyCapitalPlan(
        plan,
        amount(policy.cost_fraction),
        amount(policy.price_drift_fraction),
        canonical(proof),
    )
    venue = strategy.leverage_proof["venue"]
    if (
        now - venue["observed_at_ms"] > policy.max_fact_age_ms
        or now - venue["book_at_ms"] > policy.max_quote_age_ms
        or venue["observed_at_ms"] > facts.observed_at_ms
        or venue["book_at_ms"] > facts.observed_at_ms
    ):
        raise ValueError("SIZING_STALE_LEVERAGE_FACTS")
    if Fraction(venue["spread_fraction"]) > _fraction(
        policy.max_spread_fraction
    ) or Fraction(venue["top_notional"]) < quantity * high * _fraction(
        policy.depth_multiple
    ):
        return result("LEVERAGE_LIQUIDITY_LIMIT", **summary)
    # Protect all envelope endpoints and the typed guard's initial reference.
    # Never tighten the original stop to force a selected leverage to fit.
    for price_point in (price, low, high):
        notional = quantity * price_point
        margin_stop = max(
            Fraction(strategy.stop_fraction),
            _fraction(
                _text(
                    max(
                        Fraction(0),
                        _fraction(loss) / notional - _fraction(policy.cost_fraction),
                    ),
                    up=True,
                )
            ),
        )
        if not _safe_margin(leverage, margin_stop, notional, venue, policy):
            return result("LEVERAGE_MARGIN_UNSAFE", **summary)
    return result(
        "SIZED_WITH_EXPLICIT_RISK",
        plan,
        **summary,
        reserved_margin=_text(quantity * high / leverage, up=True),
        planned_loss=loss,
        quantity=plan.quantity,
    )
