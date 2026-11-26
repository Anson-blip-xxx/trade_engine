"""Causal, research-only 15m candidates; no sizing, persistence or venue I/O.

The public builder recomputes the existing ``trend_structure`` contract from
server-owned closed minute facts. A TradingView notification only requests that
check: its direction, strength, setup and timeframe never determine the result.
Thresholds are explicitly supplied research hypotheses, not calibrated edge.

``available_at_ms`` records the caller's observation of data availability; this
pure module cannot prove historical availability or authenticate a data source.
The caller must provide trusted server bars. The returned value grants neither
deployment permission nor order authority. UNKNOWN/NO_SETUP remain observations,
not negative signal receipts. Existing receipt/freeze recovery must happen before
this builder is invoked, never by rebuilding an already admitted opportunity.
"""

import json
from dataclasses import asdict, dataclass, field
from fractions import Fraction

from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds
from v2_core.precision import amount, decimal_text
from v2_core.strategy_entry_worker import EntrySignalState, FrozenEntryCandidate
from v2_core.strategy_execution_context import validate_execution_context
from v2_core.strategy_plan import TradePlan
from v2_core.strategy_registry import DeploymentBinding
from v2_core.trend_structure import (
    build_trend_structure,
    validate_trend_structure,
)

BUILDER_VERSION = "closed-structure-candidate-v1"
# This identity contract deliberately has its own lifetime: bumping a strategy,
# builder, setup or threshold version must not create a second opportunity.
OPPORTUNITY_SCHEMA = "binance-futures-15m-direction-v1"


@dataclass(frozen=True)
class CandidatePolicy:
    """All thresholds mandatory; ratios/fractions are not percentage scores.

    ``max_atr_pct`` uses percent units, like the reused structure contract.
    Directional flow means taker-buy share for BUY, one minus that share for
    SELL. A measured zero is a fact, not missing flow. EMA slope thresholds are
    ATR multiples per completed 15m bar. Max extension is distance from EMA20.
    Age limits are inclusive. No default silently enables a research strategy.
    """

    min_volume_ratio: str
    min_directional_taker_ratio: str
    min_ema_slope_atr: str
    min_breakout_atr: str
    max_extension_atr: str
    max_atr_pct: str
    max_structure_age_ms: int
    max_decision_delay_ms: int

    def __post_init__(self):
        numeric = (
            "min_volume_ratio",
            "min_directional_taker_ratio",
            "min_ema_slope_atr",
            "min_breakout_atr",
            "max_extension_atr",
            "max_atr_pct",
        )
        for name in numeric:
            parsed = amount(getattr(self, name))
            if parsed < 0 or (
                name in {"max_extension_atr", "max_atr_pct"} and parsed == 0
            ):
                raise ValueError("CANDIDATE_POLICY_RANGE")
            object.__setattr__(self, name, decimal_text(parsed))
        if amount(self.min_directional_taker_ratio) > 1:
            raise ValueError("CANDIDATE_POLICY_FLOW_RANGE")
        for name in ("max_structure_age_ms", "max_decision_delay_ms"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 86400000:
                raise ValueError("CANDIDATE_POLICY_TIME_RANGE")

    def payload(self):
        return asdict(self)

    def binding_config(self):
        """Explicit fragment for deployment config['candidate_builder']."""
        return {"schema": BUILDER_VERSION, "policy": self.payload()}


def _classify(structure, policy):
    if structure["status"] != "READY":
        return "UNKNOWN", "STRUCTURE_WARMUP", None, None
    values = structure["values"]
    if values["taker_buy_ratio"] is None:
        return "UNKNOWN", "TAKER_FLOW_UNAVAILABLE", None, None
    if values["volume_ratio"] is None:
        return "UNKNOWN", "VOLUME_BASELINE_UNAVAILABLE", None, None
    # Exact rational comparisons avoid ambient Decimal precision and rounding
    # thresholds. Indicators themselves come exclusively from trend_structure.
    numbers = {
        key: Fraction(amount(value))
        for key, value in values.items()
        if type(value) is str
    }
    thresholds = {
        key: Fraction(amount(value))
        for key, value in policy.payload().items()
        if type(value) is str
    }
    atr = numbers["atr"]
    if atr == 0:
        return "UNKNOWN", "ZERO_ATR", None, None
    if numbers["atr_pct"] > thresholds["max_atr_pct"]:
        return "NO_SETUP", "VOLATILITY_LIMIT", None, None
    if numbers["volume_ratio"] < thresholds["min_volume_ratio"]:
        return "NO_SETUP", "VOLUME_UNCONFIRMED", None, None
    for side, direction, suffix in (("BUY", 1, "long"), ("SELL", -1, "short")):
        close, fast, slow = (numbers[key] for key in ("close", "ema20", "ema60"))
        if not (
            direction * (fast - slow) > 0
            and direction * (close - fast) > 0
            and direction * (close - numbers["open"]) > 0
            and all(
                direction * numbers[key] >= thresholds["min_ema_slope_atr"] * atr
                for key in ("ema20_slope", "ema60_slope")
            )
        ):
            continue
        flow = numbers["taker_buy_ratio"]
        directional_flow = flow if side == "BUY" else 1 - flow
        if directional_flow < thresholds["min_directional_taker_ratio"]:
            return "NO_SETUP", "FLOW_UNCONFIRMED", None, None
        if direction * (close - fast) > thresholds["max_extension_atr"] * atr:
            return "NO_SETUP", "EXTENSION_LIMIT", None, None
        boundary = numbers["prior20_high" if side == "BUY" else "prior20_low"]
        if values[f"breakout_{suffix}"]:
            if direction * (close - boundary) < thresholds["min_breakout_atr"] * atr:
                # A failed breakout threshold cannot silently reclassify the
                # same bar as a pullback and bypass that explicit hypothesis.
                return "NO_SETUP", "BREAKOUT_BUFFER_UNCONFIRMED", None, None
            return "CANDIDATE", "CLOSED_BREAKOUT", side, "BREAKOUT"
        resumed_boundary = numbers["previous_high" if side == "BUY" else "previous_low"]
        if values[f"prior3_touched_ema20_{suffix}"] and (
            direction * (close - resumed_boundary) > 0
        ):
            return "CANDIDATE", "CLOSED_PULLBACK_RESUME", side, "PULLBACK_RESUME"
    return "NO_SETUP", "STRUCTURE_UNCONFIRMED", None, None


@dataclass(frozen=True)
class StructureCandidateDecision:
    """Detached research observation; its hash is not provenance evidence.

    Outcome fields are derived, not caller-provided. Revalidating the envelope
    cannot authenticate OHLCV/EMA facts without server bars; use build_candidate
    at the trusted data boundary, not client-supplied structure envelopes.
    """

    structure_json: str
    policy: CandidatePolicy
    available_at_ms: int
    decided_at_ms: int
    status: str = field(init=False)
    reason: str = field(init=False)
    side: str | None = field(init=False)
    setup: str | None = field(init=False)
    opportunity_id: str | None = field(init=False)

    def __post_init__(self):
        if type(self.policy) is not CandidatePolicy:
            raise TypeError("CANDIDATE_EXPLICIT_POLICY_REQUIRED")
        policy = CandidatePolicy(**self.policy.payload())
        object.__setattr__(self, "policy", policy)
        if (
            type(self.structure_json) is not str
            or len(self.structure_json.encode()) > 16384
        ):
            raise ValueError("CANDIDATE_STRUCTURE_BOUND")
        structure = json.loads(self.structure_json)
        if type(structure) is not dict:
            raise ValueError("CANDIDATE_STRUCTURE_REQUIRED")
        validate_trend_structure(
            structure,
            symbol=structure.get("symbol"),
            environment=structure.get("environment"),
            closed_at_ms=structure.get("closed_at_ms"),
        )
        object.__setattr__(self, "structure_json", canonical(structure))
        milliseconds(self.available_at_ms)
        milliseconds(self.decided_at_ms)
        if not structure["closed_at_ms"] <= self.available_at_ms <= self.decided_at_ms:
            raise ValueError("CANDIDATE_NONCAUSAL_AVAILABILITY")
        if self.decided_at_ms - self.available_at_ms > policy.max_decision_delay_ms:
            raise ValueError("CANDIDATE_DECISION_STALE")
        last_closed = structure["last_closed_at_ms"]
        if last_closed is not None and (
            self.decided_at_ms - last_closed > policy.max_structure_age_ms
        ):
            raise ValueError("CANDIDATE_STRUCTURE_STALE")
        status, reason, side, setup = _classify(structure, policy)
        for name, value in zip(
            ("status", "reason", "side", "setup"),
            (status, reason, side, setup),
            strict=True,
        ):
            object.__setattr__(self, name, value)
        opportunity = None
        if status == "CANDIDATE":
            opportunity = "structure15m:" + digest(
                canonical(
                    {
                        "schema": OPPORTUNITY_SCHEMA,
                        "exchange": "BINANCE",
                        "product": "FUTURES",
                        "environment": structure["environment"],
                        "symbol": structure["symbol"],
                        "side": side,
                        "closed_bar_ms": last_closed,
                    }
                )
            )
        object.__setattr__(self, "opportunity_id", opportunity)

    @property
    def structure(self):
        return json.loads(self.structure_json)

    @property
    def observed_at_ms(self):
        return self.structure["closed_at_ms"]

    @property
    def execution_authorized(self):
        return False

    @property
    def decision_digest(self):
        return digest(canonical(self.payload()))

    def payload(self):
        return {
            "schema": BUILDER_VERSION,
            "policy": self.policy.payload(),
            "structure": self.structure,
            "available_at_ms": self.available_at_ms,
            "decided_at_ms": self.decided_at_ms,
            "status": self.status,
            "reason": self.reason,
            "side": self.side,
            "setup": self.setup,
            "opportunity_id": self.opportunity_id,
            "execution_authorized": False,
        }


def build_candidate(
    bars,
    *,
    symbol,
    environment,
    closed_at_ms,
    available_at_ms,
    decided_at_ms,
    policy,
):
    """Recompute closed 15m structure, never infer missing facts from a signal.

    Closed 1m bars in an incomplete 15m bucket are recorded but excluded by the
    reused indicator contract. Future/unclosed minutes, gaps and malformed facts
    raise; valid insufficient history is UNKNOWN, never an OPEN candidate.
    """
    if type(policy) is not CandidatePolicy:
        raise TypeError("CANDIDATE_EXPLICIT_POLICY_REQUIRED")
    milliseconds(available_at_ms)
    milliseconds(decided_at_ms)
    milliseconds(closed_at_ms)
    if not closed_at_ms <= available_at_ms <= decided_at_ms:
        raise ValueError("CANDIDATE_NONCAUSAL_AVAILABILITY")
    structure = build_trend_structure(
        bars, symbol=symbol, environment=environment, closed_at_ms=closed_at_ms
    )
    return StructureCandidateDecision(
        canonical(structure), policy, available_at_ms, decided_at_ms
    )


def assemble_frozen_candidate(decision, *, signal, binding, plan, execution):
    """Bind a *given* sized TradePlan to verified research/signal inputs.

    This function neither creates an opportunity from a TV label nor invents
    sizing/stop/leverage. The binding must freeze the exact builder policy in
    ``config['candidate_builder']``. ``signal.now_ms`` is the caller-provided
    current verification time: no future decision, stale decision or expired
    source/plan is accepted. The complete original signal digest is preserved.

    A stored candidate or receipt must be recovered unchanged through the typed
    worker. No terminal decision is written for UNKNOWN/NO_SETUP. Registry,
    first-writer persistence, admission and runtime risk checks remain external.
    """
    if type(decision) is not StructureCandidateDecision:
        raise TypeError("CANDIDATE_TYPED_DECISION_REQUIRED")
    verified = StructureCandidateDecision(
        decision.structure_json,
        decision.policy,
        decision.available_at_ms,
        decision.decided_at_ms,
    )
    if decision.payload() != verified.payload():
        raise ValueError("CANDIDATE_DECISION_MISMATCH")
    if verified.status != "CANDIDATE":
        raise ValueError("CANDIDATE_SETUP_REQUIRED")
    if type(signal) is not EntrySignalState:
        raise TypeError("CANDIDATE_TYPED_SIGNAL_REQUIRED")
    if signal.candidate is not None or signal.receipt is not None:
        raise ValueError("CANDIDATE_RECOVERY_REQUIRED")
    # Revalidate public value objects rather than trusting a restored instance.
    signal = EntrySignalState(
        signal.signal_id,
        signal.source,
        signal.environment,
        signal.snapshot_json,
        signal.now_ms,
    )
    if type(plan) is not TradePlan or type(binding) is not DeploymentBinding:
        raise TypeError("CANDIDATE_TYPED_PLAN_AND_BINDING_REQUIRED")
    plan = TradePlan.from_payload(plan.payload())
    structure, snapshot = verified.structure, signal.snapshot
    if snapshot["signal"] == "EVENT_END":
        raise ValueError("CANDIDATE_NON_ENTRY_SIGNAL")
    if (
        plan.scope != binding.scope
        or (plan.scope.exchange, plan.scope.product) != ("BINANCE", "FUTURES")
        or plan.scope.environment != structure["environment"]
        or signal.environment != structure["environment"]
        or snapshot["symbol"] != structure["symbol"]
        or plan.symbol != structure["symbol"]
        or plan.side != verified.side
        or plan.opportunity_id != verified.opportunity_id
    ):
        raise ValueError("CANDIDATE_PLAN_SCOPE_OR_OPPORTUNITY_MISMATCH")
    if binding.config.get("candidate_builder") != verified.policy.binding_config():
        raise ValueError("CANDIDATE_DEPLOYMENT_POLICY_MISMATCH")
    if not (
        snapshot["observed_at"] <= plan.observed_at_ms == verified.observed_at_ms
        and plan.decided_at_ms == verified.decided_at_ms <= signal.now_ms
        and signal.now_ms < plan.expires_at_ms <= snapshot["expires_at_ms"]
    ):
        raise ValueError("CANDIDATE_SIGNAL_OR_PLAN_TIME_MISMATCH")
    if (
        signal.now_ms - verified.decided_at_ms > verified.policy.max_decision_delay_ms
        or signal.now_ms - structure["last_closed_at_ms"]
        > verified.policy.max_structure_age_ms
    ):
        raise ValueError("CANDIDATE_ASSEMBLY_STALE")
    execution = validate_execution_context(plan, execution)
    return FrozenEntryCandidate(
        binding=binding,
        signal_id=signal.signal_id,
        source=signal.source,
        signal_digest=digest(canonical(snapshot)),
        action="OPEN",
        rationale=f"{BUILDER_VERSION}:{verified.reason}:{verified.side}",
        observed_at_ms=plan.observed_at_ms,
        decided_at_ms=plan.decided_at_ms,
        expires_at_ms=plan.expires_at_ms,
        features_json=canonical(
            {"candidate_builder": verified.payload(), "execution": execution}
        ),
        plan=plan,
    )
