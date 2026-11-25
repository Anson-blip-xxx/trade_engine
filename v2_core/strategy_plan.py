"""Immutable strategy plans and purpose-specific legacy protection reads.

These objects grant no execution authority and perform no I/O. New-schema plans
are not wired into admission, dispatch or position management in this release.
Legacy stops keep their original contract: entry expiry, current policy, model
availability and research-only fields cannot disable an existing protection.
"""

import json
import re
from dataclasses import asdict, dataclass, fields
from fractions import Fraction

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.ingress import identity, milliseconds, symbol
from v2_core.ledger import amount
from v2_core.protection import ProtectionSpec
from v2_core.strategy_registry import deployment_generation, strategy_identifier

PLAN_SCHEMA = "strategy-trade-plan-v1"
LEGACY_DIRECTIONAL_VERSION = "directional-admission-v2-1"
MAX_EXIT_CONFIG_BYTES = 65536


def _json_object(encoded):
    if type(encoded) is not str:
        raise ValueError("STRATEGY_PLAN_CONFIG_BOUND")
    try:
        size = len(encoded.encode("utf-8"))
    except UnicodeError:
        raise ValueError("STRATEGY_PLAN_INVALID_CONFIG") from None
    if size > MAX_EXIT_CONFIG_BYTES:
        raise ValueError("STRATEGY_PLAN_CONFIG_BOUND")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("STRATEGY_PLAN_DUPLICATE_FIELD")
            result[key] = value
        return result

    try:
        normalized = canonical(json.loads(encoded, object_pairs_hook=unique))
    except (ValueError, TypeError, RecursionError):
        raise ValueError("STRATEGY_PLAN_INVALID_CONFIG") from None
    if len(normalized.encode("utf-8")) > MAX_EXIT_CONFIG_BYTES:
        raise ValueError("STRATEGY_PLAN_CONFIG_BOUND")
    return normalized


@dataclass(frozen=True)
class TradePlan:
    """A validated plan value, not a reservation, deployment permit or order.

    ``planned_loss`` includes the immutable initial planning budget. It is not a
    promise that a stop can fill at its trigger price. Runtime venue filters,
    risk reservation and deployment fencing remain separate mandatory gates.
    """

    scope: AccountScope
    instance_id: str
    strategy_id: str
    code_version: str
    deployment_generation: int
    config_digest: str
    opportunity_id: str
    symbol: str
    side: str
    quantity: str
    entry_price: str
    stop_price: str
    planned_loss: str
    quantity_step: str
    leverage: int
    margin_mode: str
    observed_at_ms: int
    decided_at_ms: int
    expires_at_ms: int
    exit_handler: str
    exit_version: str
    exit_config_json: str
    schema_version: str = PLAN_SCHEMA

    def __post_init__(self):
        if type(self.scope) is not AccountScope:
            raise TypeError("STRATEGY_PLAN_ACCOUNT_SCOPE_REQUIRED")
        object.__setattr__(self, "scope", AccountScope(**asdict(self.scope)))
        if self.schema_version != PLAN_SCHEMA:
            raise ValueError("STRATEGY_PLAN_SCHEMA_UNSUPPORTED")
        for value in (
            self.instance_id,
            self.strategy_id,
            self.code_version,
            self.exit_handler,
            self.exit_version,
        ):
            strategy_identifier(value)
        identity(self.opportunity_id)
        if not isinstance(self.config_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.config_digest
        ):
            raise ValueError("STRATEGY_PLAN_CONFIG_DIGEST_REQUIRED")
        deployment_generation(self.deployment_generation)
        symbol(self.symbol)
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("STRATEGY_PLAN_EXPLICIT_SIDE_REQUIRED")
        if type(self.leverage) is not int or not 1 <= self.leverage <= 125:
            raise ValueError("STRATEGY_PLAN_INVALID_LEVERAGE")
        if self.margin_mode not in {"ISOLATED", "CROSSED"}:
            raise ValueError("STRATEGY_PLAN_INVALID_MARGIN_MODE")
        for value in (self.observed_at_ms, self.decided_at_ms, self.expires_at_ms):
            milliseconds(value)
        if not (
            self.observed_at_ms
            <= self.decided_at_ms
            < self.expires_at_ms
            <= self.observed_at_ms + 86400000
        ):
            raise ValueError("STRATEGY_PLAN_INVALID_LIFETIME")
        quantity, entry, stop, risk, step = (
            Fraction(amount(value, positive=True))
            for value in (
                self.quantity,
                self.entry_price,
                self.stop_price,
                self.planned_loss,
                self.quantity_step,
            )
        )
        distance = entry - stop if self.side == "BUY" else stop - entry
        if distance <= 0 or risk < quantity * distance:
            raise ValueError("STRATEGY_PLAN_INVALID_INITIAL_RISK")
        if (quantity / step).denominator != 1:
            raise ValueError("STRATEGY_PLAN_QUANTITY_STEP_MISMATCH")
        object.__setattr__(
            self, "exit_config_json", _json_object(self.exit_config_json)
        )

    @property
    def execution_authorized(self):
        return False

    @property
    def exit_side(self):
        return "SELL" if self.side == "BUY" else "BUY"

    @property
    def exit_config(self):
        return json.loads(self.exit_config_json)

    @property
    def plan_digest(self):
        return digest(canonical(self.payload()))

    def payload(self):
        return asdict(self)

    def entry_valid_at(self, now_ms):
        """Only a time predicate; an expired entry still retains its exit plan."""
        milliseconds(now_ms)
        return self.decided_at_ms <= now_ms < self.expires_at_ms

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict) or set(payload) != {
            field.name for field in fields(cls)
        }:
            raise ValueError("STRATEGY_PLAN_FIELDS_REQUIRED")
        # Copy and reject credentials/floats before interpreting external data.
        try:
            copied = json.loads(canonical(payload))
            copied["scope"] = AccountScope(**copied["scope"])
            return cls(**copied)
        except (ValueError, TypeError, RecursionError):
            raise ValueError("STRATEGY_PLAN_INVALID_PAYLOAD") from None


@dataclass(frozen=True)
class LegacyDirectionalPlan:
    """Minimal, immutable projection for already-admitted legacy positions.

    No future registry metadata is fabricated. The legacy software exit still
    reads current account policy; this projection does not freeze or migrate it.
    Only fields needed by protection are required. Complete strategy validation
    for opening, software exits and settlement remains with those consumers.
    ``read`` must receive committed evidence from the scoped order reader, and
    ``protection_spec`` must receive that reader's order snapshot. This value is
    not proof of storage provenance or permission to install a protection.
    """

    scope: AccountScope
    strategy: str
    producer: str
    strategy_version: str
    symbol: str
    projection_json: str

    def __post_init__(self):
        if type(self.scope) is not AccountScope:
            raise TypeError("STRATEGY_PLAN_ACCOUNT_SCOPE_REQUIRED")
        object.__setattr__(self, "scope", AccountScope(**asdict(self.scope)))
        if (
            self.strategy_version != LEGACY_DIRECTIONAL_VERSION
            or self.strategy not in {"S6", "S8"}
            or self.producer != self.strategy.lower()
        ):
            raise ValueError("DIRECTIONAL_STOP_EVIDENCE_REQUIRED")

    @classmethod
    def read(cls, *, scope, config, snapshot, producer, strategy_version):
        if type(scope) is not AccountScope:
            raise TypeError("STRATEGY_PLAN_ACCOUNT_SCOPE_REQUIRED")
        if (
            strategy_version != LEGACY_DIRECTIONAL_VERSION
            or config.get("mode") != "TESTNET_ADMISSION_ONLY"
            or config.get("account_scope") != asdict(scope)
            or config.get("strategy") not in {"S6", "S8"}
            or producer != config["strategy"].lower()
        ):
            raise ValueError("DIRECTIONAL_STOP_EVIDENCE_REQUIRED")
        evaluation = snapshot["features"]["evaluation"]
        # Avoid retaining complete market history or adding new mandatory fields
        # to a protection read. Canonical copies cannot be changed by callers.
        projection = {
            "admission_authorized": evaluation.get("admission_authorized"),
            "execution_market_gate": evaluation.get("execution_market_gate"),
            "market_plan": {
                key: evaluation["market_plan"][key] for key in ("strategy", "side")
            },
            "sizing": {
                key: evaluation["sizing"][key]
                for key in ("reason", "quantity", "stop_price")
            },
            "result": {
                key: snapshot["result"][key] for key in ("action", "side", "quantity")
            },
        }
        return cls(
            scope=scope,
            strategy=config["strategy"],
            producer=producer,
            strategy_version=strategy_version,
            symbol=snapshot["symbol"],
            projection_json=canonical(projection),
        )

    @property
    def exit_policy_binding(self):
        return "LEGACY_CURRENT_ACCOUNT_POLICY"

    @property
    def execution_authorized(self):
        return False

    def protection_spec(self, order):
        evaluation = json.loads(self.projection_json)
        market, sizing, result = (
            evaluation["market_plan"],
            evaluation["sizing"],
            evaluation["result"],
        )
        expected_side = "BUY" if self.strategy == "S6" else "SELL"
        if (
            evaluation.get("admission_authorized") is not True
            or evaluation.get("execution_market_gate") != "MARKET_GATES_PASSED"
            or market["strategy"] != self.strategy
            or market["side"] != ("LONG" if expected_side == "BUY" else "SHORT")
            or sizing["reason"] != "SIZED"
            or result["action"] != "OPEN"
            or result["side"] != expected_side
            or order["side"] != expected_side
            or self.symbol != order["symbol"]
            or amount(sizing["quantity"], positive=True)
            != amount(order["intent"]["quantity"], positive=True)
            or amount(result["quantity"], positive=True)
            != amount(sizing["quantity"], positive=True)
        ):
            raise ValueError("DIRECTIONAL_STOP_EVIDENCE_MISMATCH")
        return ProtectionSpec(
            order["episode_id"],
            order["symbol"],
            "SELL" if expected_side == "BUY" else "BUY",
            "STOP_MARKET",
            sizing["stop_price"],
        )
