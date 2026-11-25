"""Typed historical lifecycle identity, never an opening or deployment permit.

The pure trace reader is for bound journal consumers; committers separately use
read_trade_plan on the transaction's connection to verify persisted bindings.
"""

from dataclasses import asdict

from v2_core.strategy_plan import TradePlan
from v2_core.strategy_plan_store import read_trade_plan_record

STRATEGY_SETTLEMENT_SOURCE = "strategy-settlement-v1"
STRATEGY_OUTCOME_SCHEMA = "strategy-outcome-v1"

# A present but malformed new marker must be selected, then diagnosed. Never
# silently lose an obligation because its plan cannot currently be decoded.
LIFECYCLE_EPISODE_SQL = """(
 (i.producer IN ('s6','s8') AND i.strategy_version='directional-admission-v2-1')
 OR EXISTS (SELECT 1 FROM v2_decision_evidence lifecycle_evidence
   WHERE lifecycle_evidence.evidence_ref=i.evidence_ref
   AND lifecycle_evidence.snapshot ? 'strategy_plan')
)"""


def strategy_plan_from_trace(trace):
    snapshot = trace.get("decision")
    if not isinstance(snapshot, dict):
        # Legacy pure analyzer fixtures did not need decision features.
        return None
    if "strategy_plan" not in snapshot:
        return None
    try:
        request = trace["request"]
        scope = {
            name: trace["scope"][name]
            for name in ("exchange", "account_id", "environment", "product")
        }
        plan = read_trade_plan_record(
            {
                "scope": scope,
                "producer": trace["scope"]["producer"],
                "strategy_version": request["strategy_version"],
                "config_digest": request["config_digest"],
                "payload": request,
                "evidence_strategy_version": request["strategy_version"],
                "evidence_config_digest": request["config_digest"],
                "config": trace["configuration"],
                "snapshot": snapshot,
            }
        )
        if (plan.scope.exchange, plan.scope.environment, plan.scope.product) != (
            "BINANCE",
            "SANDBOX",
            "FUTURES",
        ) or not plan.symbol.endswith("USDT"):
            raise ValueError("unsupported strategy cash scope")
        return plan
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("STRATEGY_LIFECYCLE_PLAN_MISMATCH") from None


def require_cash_subject(trace):
    plan = strategy_plan_from_trace(trace)
    if plan is None and (
        trace["configuration"].get("mode") != "TESTNET_ADMISSION_ONLY"
        or trace["scope"].get("producer") not in {"s6", "s8"}
    ):
        raise ValueError("DIRECTIONAL_EVIDENCE_REQUIRED")
    return plan


def plan_provenance(plan):
    if type(plan) is not TradePlan:
        raise ValueError("STRATEGY_LIFECYCLE_PLAN_REQUIRED")
    return {
        "schema": "strategy-plan-provenance-v1",
        "plan_digest": plan.plan_digest,
        "instance_id": plan.instance_id,
        "strategy_id": plan.strategy_id,
        "code_version": plan.code_version,
        "config_digest": plan.config_digest,
        "deployment_generation": plan.deployment_generation,
    }


def cash_namespace(plan):
    return "strategy-cash-audit-v1" if plan is not None else "directional-cash-audit-v1"


def settlement_source(plan):
    return (
        STRATEGY_SETTLEMENT_SOURCE if plan is not None else "directional-settlement-v1"
    )


def validate_settlement_provenance(plan, evidence):
    if not isinstance(evidence, dict) or evidence.get("source") != settlement_source(
        plan
    ):
        raise ValueError("STRATEGY_SETTLEMENT_PROVENANCE_MISMATCH")
    if plan is not None:
        policy = evidence.get("cash_policy_snapshot")
        if (
            evidence.get("strategy_plan") != plan_provenance(plan)
            or evidence.get("account_scope") != asdict(plan.scope)
            or not isinstance(evidence.get("cash_basis"), dict)
            or not isinstance(policy, dict)
            or not isinstance(policy.get("values"), dict)
            or type(policy["values"].get("settlement.cash_basis_version")) is not int
            or policy["values"]["settlement.cash_basis_version"] != 2
        ):
            raise ValueError("STRATEGY_SETTLEMENT_PROVENANCE_MISMATCH")
    if plan is None and "strategy_plan" in evidence:
        raise ValueError("STRATEGY_SETTLEMENT_PROVENANCE_MISMATCH")
