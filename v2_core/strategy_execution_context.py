"""Frozen, plan-bound inputs for typed opening checks; never an entry permit.

The context contains only shared safety inputs. It does not invent directional
strategy identities or read current policy while replaying a stored decision.
"""

import json
from dataclasses import asdict
from uuid import UUID

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical
from v2_core.ledger import amount
from v2_core.strategy_plan import TradePlan
from v2_core.strategy_plan_store import read_trade_plan_record, trade_plan_record

SCHEMA = "strategy-opening-context-v1"
POLICY_FIELDS = frozenset(
    {
        "entry.reentry_enabled",
        "entry.price_guard_enabled",
        "entry.max_adverse_price_fraction",
        "entry.max_stop_risk_increase",
    }
)
_REQUIRED = frozenset(
    {
        "schema",
        "account_scope",
        "symbol",
        "plan_digest",
        "entry_controls",
        "liquidity_controls",
        "policy",
    }
)
_OPTIONAL = frozenset({"liquidity_evidence", "execution_quote"})


def validate_execution_context(plan, context):
    """Validate and detach one context without granting execution authority."""
    try:
        if type(plan) is not TradePlan or type(context) is not dict:
            raise ValueError()
        plan = TradePlan.from_payload(plan.payload())
        if (
            not _REQUIRED <= context.keys()
            or context.keys() - _REQUIRED - _OPTIONAL
            or context["schema"] != SCHEMA
            or context["account_scope"] != asdict(plan.scope)
            or context["symbol"] != plan.symbol
            or context["plan_digest"] != plan.plan_digest
            or type(context["policy"]) is not dict
            or set(context["policy"]) != POLICY_FIELDS
            or any(
                type(context[name]) is not dict for name in _OPTIONAL & context.keys()
            )
        ):
            raise ValueError()
        from v2_core.entry_controls import from_snapshot as entry_snapshot
        from v2_core.liquidity_controls import from_snapshot as liquidity_snapshot
        from v2_core.runtime_policy import resolve

        entry_snapshot(context["entry_controls"], plan.scope)
        liquidity_snapshot(context["liquidity_controls"], plan.scope)
        # Resolve validates the four values against the shared policy bounds;
        # only the supplied four fields are retained in this frozen context.
        resolve(context["policy"])
        encoded = canonical(context)
        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError()
        return json.loads(encoded)
    except (ValueError, TypeError, KeyError, RecursionError, ArithmeticError):
        raise ValueError("STRATEGY_EXECUTION_CONTEXT_INVALID") from None


def execution_context(
    plan,
    *,
    entry_controls,
    liquidity_controls,
    policy,
    liquidity_evidence=None,
    execution_quote=None,
):
    """Build a detached context from explicitly supplied frozen safety inputs."""
    if type(plan) is not TradePlan:
        raise ValueError("STRATEGY_EXECUTION_CONTEXT_INVALID")
    context = {
        "schema": SCHEMA,
        "account_scope": asdict(plan.scope),
        "symbol": plan.symbol,
        "plan_digest": plan.plan_digest,
        "entry_controls": entry_controls,
        "liquidity_controls": liquidity_controls,
        "policy": policy,
    }
    for name, value in (
        ("liquidity_evidence", liquidity_evidence),
        ("execution_quote", execution_quote),
    ):
        if value is not None:
            context[name] = value
    return validate_execution_context(plan, context)


def read_execution_context(conn, scope, order_id):
    """Read an OPEN order's committed typed context; only absent plans are legacy.

    Caller owns the account submission lock. Entry freshness, deployment,
    reservations and current control comparisons remain independent checks.
    """
    if type(scope) is not AccountScope:
        raise ValueError("STRATEGY_EXECUTION_SCOPE_REQUIRED")
    row = conn.execute(
        "SELECT i.exchange,i.account_id,i.environment,i.product,i.producer,"
        "i.strategy_version,i.config_digest,i.payload,e.strategy_version,"
        "e.config_digest,"
        "CASE WHEN e.snapshot ? 'strategy_plan' THEN e.config ELSE '{}'::jsonb END,"
        "CASE WHEN e.snapshot ? 'strategy_plan' THEN e.snapshot ELSE '{}'::jsonb END,"
        "o.leg,o.quantity::text "
        "FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id "
        "JOIN v2_decision_evidence e USING(evidence_ref) WHERE o.order_id=%s "
        "AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)",
        (str(UUID(str(order_id))), *asdict(scope).values()),
    ).fetchone()
    if row is None:
        raise ValueError("STRATEGY_EXECUTION_ORDER_MISSING")
    plan = read_trade_plan_record(trade_plan_record(row[:12]))
    if plan is None:
        return None
    if (
        plan.scope != scope
        or row[12] != "OPEN"
        or amount(row[13], positive=True) != amount(plan.quantity, positive=True)
    ):
        raise ValueError("STRATEGY_EXECUTION_ORDER_MISMATCH")
    features = row[11].get("features")
    if type(features) is not dict or "execution" not in features:
        raise ValueError("STRATEGY_EXECUTION_CONTEXT_REQUIRED")
    return plan, validate_execution_context(plan, features["execution"])
