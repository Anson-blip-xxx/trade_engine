"""Bounded, read-only UTC+8 closed-episode accounting before mover selection.

The daily population starts at actual CLOSE fills, never an entry-time lookback
or a winner-symbol list. Only current complete settlement proofs are reported;
an unavailable or capped query is not an empty account or a complete day.
"""

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import asdict
from decimal import Decimal, InvalidOperation, localcontext
from statistics import median
from uuid import UUID

from services.v2_daily_mover_execution_review import build_execution_review
from services.v2_reentry_evaluation import outcomes
from v2_core.account_risk import AccountScope
from v2_core.execution_cost_evidence import (
    build_execution_cost_case,
    empty_execution_cost_review,
)
from v2_core.jev_advisory import _decimal as _jev_decimal
from v2_core.jev_entry_gate import BASELINE_FALLBACK_CAUSES
from v2_core.symbols import is_supported_symbol

VERSION = "daily-account-review-v1"
JEV_VERSION = "daily-account-jev-participation-v1"
MAX_TRADES = 500
MAX_READ_SECONDS = 20
DAY_MS = 86_400_000
_OFFSET_MS = 28_800_000
_SPLIT_BASIS = "VERIFIED_RANKING_EPISODE_REFS_NOT_SYMBOL_MEMBERSHIP"
_DEFINITE_EXCLUSIONS = {
    "MAINTENANCE_EXCLUDED",
    "NOT_CLOSED_IN_REVIEW_DAY",
    "OPEN_OR_PARTIAL_EXIT",
}
_LABEL_STATES = {
    "UNLINKED_DECISION",
    "NO_INTENT",
    "MAINTENANCE_EXCLUDED",
    "LATER_ORDER_STATE",
    "INCONSISTENT_FILLS",
    "UNFILLED",
    "OPEN_OR_PARTIAL_EXIT",
    "AWAITING_TERMINAL_ORDERS",
    "AWAITING_SETTLEMENT",
    "SETTLEMENT_EVIDENCE_INCOMPLETE",
    "SETTLEMENT_REVISION_CHANGED",
}
_RETRYABLE_SETTLEMENT_STATES = {
    "AWAITING_TERMINAL_ORDERS",
    "AWAITING_SETTLEMENT",
    "SETTLEMENT_EVIDENCE_INCOMPLETE",
    "SETTLEMENT_REVISION_CHANGED",
    "LABEL_UNAVAILABLE",
    "LATER_FILL_OR_ORDER_STATE",
    "LATER_ORDER_STATE",
}
_NONRETRYABLE_READ_ERRORS = {"READ_ONLY_REQUIRED", "STABLE_SNAPSHOT_REQUIRED"}
_JEV_GROUPS = (
    "MODEL_APPROVED",
    "MODEL_REJECTED",
    "FALLBACK",
    "DISABLED",
    "NOT_MODEL_EVALUATED",
    "SHADOW_ONLY",
    "UNKNOWN",
)
_JEV_SHADOW_STATUSES = {
    "SUBMITTED_NOT_DURABLE",
    "SUBMISSION_UNAVAILABLE",
    "OBSERVER_UNAVAILABLE",
    "INVALID_CANDIDATE",
    "DISABLED",
    "STOPPED",
    "QUEUE_BUSY",
    "QUEUE_FULL",
}
_JEV_SKIP_REASONS = {
    "JEV_RESULT_UNAVAILABLE",
    "JEV_BASELINE_CONTEXT_EXPIRED",
    "JEV_ORIGINAL_ADMISSION_INVALID",
    "JEV_FIXED_DEADLINE_INVALID",
    "JEV_CANDIDATE_BINDING_MISMATCH",
    "JEV_REVIEW_DEADLINE_EXPIRED",
    "JEV_RESULT_INVALID",
    "JEV_GATE_POLICY_INVALID",
    "JEV_CANDIDATE_POLICY_CHANGED",
    "JEV_MODEL_TTL_TOO_SHORT",
    "JEV_MARKET_SNAPSHOT_EXPIRED",
    "JEV_CANDIDATE_BINDING_INVALID",
    "JEV_REVIEW_UNAVAILABLE",
}

# The sentinel limits distinct episode identities, not close-fill rows. No
# opening-time lower bound: months-old carry-in belongs to its final-close day.
_CANDIDATES_SQL = """SELECT DISTINCT i.intent_id::text
FROM v2_trade_intents i
JOIN v2_orders o ON o.episode_id=i.intent_id AND o.leg='CLOSE'
JOIN v2_fills f ON f.order_id=o.order_id
WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
  AND i.producer IN ('s6','s8')
  AND i.created_at<=to_timestamp(%s/1000.0)
  AND f.occurred_at_ms>=%s AND f.occurred_at_ms<%s
  AND f.created_at<=to_timestamp(%s/1000.0)
ORDER BY i.intent_id::text LIMIT 501"""

# Full episode aggregates are recomputed independently of a materialized
# directional outcome. Later facts conservatively invalidate historical proof.
# The running balance also rejects a close-before-open path with equal totals.
_DETAILS_SQL = """SELECT i.intent_id::text,i.payload->>'symbol',
  i.payload->>'side',(extract(epoch FROM i.created_at)*1000)::bigint,
  f.oq::text,f.cq::text,f.onot::text,f.cnot::text,f.fees::text,
  f.opened_ms,f.closed_ms,f.fills_valid,f.all_known,
  ord.terminal,ord.known,ord.maintenance
FROM v2_trade_intents i
LEFT JOIN LATERAL (
  SELECT sum(z.quantity) FILTER(WHERE z.leg='OPEN') oq,
    sum(z.quantity) FILTER(WHERE z.leg='CLOSE') cq,
    sum(z.quantity*z.price) FILTER(WHERE z.leg='OPEN') onot,
    sum(z.quantity*z.price) FILTER(WHERE z.leg='CLOSE') cnot,
    CASE WHEN bool_and(z.fee_currency='USDT') THEN sum(z.fee) END fees,
    min(z.occurred_at_ms) FILTER(WHERE z.leg='OPEN') opened_ms,
    max(z.occurred_at_ms) FILTER(WHERE z.leg='CLOSE') closed_ms,
    bool_and(z.quantity>0 AND z.price>0 AND z.balance>=0) fills_valid,
    bool_and(z.occurred_at_ms<=%s
      AND z.created_at<=to_timestamp(%s/1000.0)) all_known
  FROM (
    SELECT o.leg,f.quantity,f.price,f.fee,f.fee_currency,
      f.occurred_at_ms,f.created_at,
      sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END)
        OVER (ORDER BY f.occurred_at_ms,
          CASE WHEN o.leg='OPEN' THEN 0 ELSE 1 END,f.fill_key) balance
    FROM v2_orders o JOIN v2_fills f USING(order_id)
    WHERE o.episode_id=i.intent_id) z) f ON true
LEFT JOIN LATERAL (
  SELECT bool_and(o.status IN ('FILLED','CANCELLED','REJECTED','RECONCILED')) terminal,
    bool_and(o.updated_at<=to_timestamp(%s/1000.0)) known,
    bool_or(coalesce(o.request_evidence->>'origin','') IN
      ('APPROVED_TESTNET_MAINTENANCE','USER_APPROVED_MAINTENANCE')) maintenance
  FROM v2_orders o WHERE o.episode_id=i.intent_id) ord ON true
WHERE i.intent_id=ANY(%s::uuid[])
  AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
  AND i.producer IN ('s6','s8')
ORDER BY i.intent_id LIMIT 500"""


# Optional, single-batch projection of already verified episodes. No snapshots,
# configs, account credentials or unrelated orders are loaded/exported. Parent
# trigger proof is tied to the registered native child, not a reason string.
# Every source, including quant, sizes at the frozen directional current price;
# a signal bar's close is not a fallback for a missing sizing reference.
_COST_SQL = """SELECT i.intent_id::text,jsonb_build_object(
  'evidence_present',e.evidence_ref IS NOT NULL,
  'planned_entry_price',e.snapshot#>>'{features,context,directional,price}',
  'initial_stop_price',e.snapshot#>>'{features,evaluation,sizing,stop_price}',
  'intent_quantity',i.payload->>'quantity',
  'sizing_quantity',e.snapshot#>>'{features,evaluation,sizing,quantity}',
  'result_quantity',e.snapshot#>>'{result,quantity}',
  'sizing_reason',e.snapshot#>>'{features,evaluation,sizing,reason}',
  'result_action',e.snapshot#>>'{result,action}',
  'result_side',e.snapshot#>>'{result,side}',
  'cost_buffer_fraction',e.snapshot#>>'{features,evaluation,sizing_explanation,cost_buffer_fraction}',
  'budgeted_stop_risk_usdt',e.snapshot#>>'{features,evaluation,sizing_explanation,budgeted_stop_risk}',
  'close_order_count',cl.n,
  'native_close_count',cl.native_n,
  'native_trigger_price',CASE WHEN cl.trigger_min=cl.trigger_max
    THEN cl.trigger_min END,
  'native_stop_type_verified',cl.trigger_verified)
FROM v2_trade_intents i
LEFT JOIN v2_decision_evidence e
  ON (e.evidence_ref,e.config_digest,e.strategy_version)=
     (i.evidence_ref,i.config_digest,i.strategy_version)
  AND e.signal_id IS NOT DISTINCT FROM i.signal_id
  AND e.snapshot->>'symbol'=i.payload->>'symbol'
  AND e.created_at<=i.created_at
  AND e.created_at<=to_timestamp(%s/1000.0)
LEFT JOIN LATERAL (
  SELECT count(*) n,
    count(*) FILTER(WHERE o.request_evidence->>'origin'='BINANCE_ALGO_CHILD') native_n,
    min(pe.evidence#>>'{parent_observation,triggerPrice}') trigger_min,
    max(pe.evidence#>>'{parent_observation,triggerPrice}') trigger_max,
    bool_and(coalesce(
      pe.evidence#>>'{parent_observation,orderType}'='STOP_MARKET'
      AND pe.evidence#>>'{parent_observation,algoId}'=o.request_evidence->>'parent_algo_id'
      AND pe.evidence#>>'{parent_observation,clientAlgoId}'=o.request_evidence->>'parent_client_algo_id'
      AND pe.evidence#>>'{parent_observation,actualOrderId}'=o.request_evidence->>'exchange_order_id'
      AND pe.evidence#>>'{native_child_binding,origin}'='BINANCE_ALGO_CHILD'
      AND pe.evidence#>>'{native_child_binding,exchange_order_id}'=o.request_evidence->>'exchange_order_id',false)) trigger_verified
  FROM v2_orders o
  LEFT JOIN LATERAL (
    SELECT oe.evidence FROM v2_order_events oe
    WHERE oe.order_id=o.order_id AND oe.evidence ? 'parent_observation'
      AND oe.created_at<=to_timestamp(%s/1000.0)
    ORDER BY oe.version DESC LIMIT 1) pe ON true
  WHERE o.episode_id=i.intent_id AND o.leg='CLOSE'
    AND EXISTS(SELECT 1 FROM v2_fills f WHERE f.order_id=o.order_id)
) cl ON true
WHERE i.intent_id=ANY(%s::uuid[])
  AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
  AND i.producer IN ('s6','s8')
  AND i.created_at<=to_timestamp(%s/1000.0)
ORDER BY i.intent_id LIMIT 500"""

# Only the already verified episodes are read. Keep this optional projection
# separate from the 16-field accounting query: an unavailable evidence table
# must not discard verified settlement totals. Never load prompts or config.
_JEV_SQL = """SELECT i.intent_id::text,e.evidence_ref IS NOT NULL,
  e.snapshot->>'source',
  e.snapshot#>>'{features,evaluation,jev_entry_gate,schema}',
  e.snapshot#>>'{features,evaluation,jev_entry_gate,status}',
  e.snapshot#>>'{features,evaluation,jev_entry_gate,reason}',
  e.snapshot#>>'{features,evaluation,jev_entry_gate,fallback_cause}',
  e.snapshot#>'{features,evaluation,jev_entry_gate,policy,enabled}',
  e.snapshot#>>'{features,evaluation,jev_entry_gate,policy,unavailable_action}',
  e.snapshot#>>'{features,evaluation,jev_entry_gate,hypotheses,trend_supported}',
  e.snapshot#>>'{features,evaluation,jev_entry_gate,hypotheses,chase_risk}',
  e.snapshot#>>'{features,evaluation,jev_entry_gate,policy,min_trend_support}',
  e.snapshot#>>'{features,evaluation,jev_entry_gate,policy,max_chase_risk}',
  jsonb_build_object(
    'shadow_present',coalesce(e.snapshot#>'{features,evaluation}' ? 'jev_shadow',false),
    'gate_present',coalesce(e.snapshot#>'{features,evaluation}' ? 'jev_entry_gate',false),
    'signal_id',i.signal_id::text,
    'consumer_scope',jsonb_build_array(i.exchange,i.account_id,i.environment,i.product,i.producer),
    'receipt',jsonb_build_object(
      'schema',e.snapshot#>'{features,evaluation,jev_shadow,schema}',
      'status',e.snapshot#>'{features,evaluation,jev_shadow,status}',
      'coverage',e.snapshot#>'{features,evaluation,jev_shadow,coverage}',
      'decision_effect',e.snapshot#>'{features,evaluation,jev_shadow,decision_effect}',
      'execution_authorized',e.snapshot#>'{features,evaluation,jev_shadow,execution_authorized}',
      'risk_override',e.snapshot#>'{features,evaluation,jev_shadow,risk_override}',
      'admission_authorized',e.snapshot#>'{features,evaluation,jev_shadow,admission_authorized}',
      'signal_id',e.snapshot#>'{features,evaluation,jev_shadow,signal_id}',
      'consumer',e.snapshot#>'{features,evaluation,jev_shadow,consumer}',
      'candidate_digest',e.snapshot#>'{features,evaluation,jev_shadow,candidate_digest}'))
FROM v2_trade_intents i
LEFT JOIN v2_decision_evidence e
  ON (e.evidence_ref,e.config_digest,e.strategy_version)=
     (i.evidence_ref,i.config_digest,i.strategy_version)
  AND e.signal_id IS NOT DISTINCT FROM i.signal_id
  AND e.snapshot->>'symbol'=i.payload->>'symbol'
  AND e.created_at<=to_timestamp(%s/1000.0)
WHERE i.intent_id=ANY(%s::uuid[])
  AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
  AND i.producer IN ('s6','s8')
  AND i.created_at<=to_timestamp(%s/1000.0)
ORDER BY i.intent_id LIMIT 500"""


def _digest(value, *, ensure_ascii=False):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=ensure_ascii,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _stamp(value):
    return type(value) is int and 0 <= value < 2**53


def _number(value):
    if type(value) not in (str, int, Decimal) or len(str(value)) > 100:
        raise ValueError("INVALID_ACCOUNTING_NUMBER")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("INVALID_ACCOUNTING_NUMBER") from None
    if (
        not number.is_finite()
        or abs(number.as_tuple().exponent) > 40
        or len(number.as_tuple().digits) > 60
        or number.copy_abs() > Decimal("1e18")
    ):
        raise ValueError("INVALID_ACCOUNTING_NUMBER")
    return number


def _text(value):
    return None if value is None else format(value, "f") if value else "0"


def _seal(result):
    result["content_digest"] = _digest(result)
    return result


def _empty(scope, start, end, cutoff):
    result = {
        "version": VERSION,
        "status": "UNKNOWN",
        "reason": "ACCOUNT_REVIEW_UNAVAILABLE",
        "scope_key": _digest(asdict(scope), ensure_ascii=True)
        if isinstance(scope, AccountScope)
        else None,
        "account_environment": scope.environment
        if isinstance(scope, AccountScope)
        else "UNKNOWN",
        "window_start_ms": start if _stamp(start) else None,
        "window_end_ms": end if _stamp(end) else None,
        "as_of_ms": cutoff if _stamp(cutoff) else None,
        "timezone": "Asia/Shanghai",
        "basis": "WHOLE_EPISODE_CURRENT_COMPLETE_SETTLEMENT_BY_LAST_CLOSE_DAY",
        "research_only": True,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "profitability_proven": False,
        "totals": None,
        "by_side": {},
        "trades": [],
        "ranking_vs_other": {
            "status": "UNKNOWN",
            "reason": "VERIFIED_RANKING_REVIEW_UNAVAILABLE",
            "basis": _SPLIT_BASIS,
            "ranking": None,
            "other": None,
        },
        "coverage": {
            "query_complete": False,
            "retryable": False,
            "candidate_count": None,
            "candidate_limit": MAX_TRADES,
            "truncated": False,
            "verified_settled_count": 0,
            "excluded_reasons": {},
            "full_closed_settlement_population_proven": False,
        },
        "limitations": [
            "DATABASE_VERIFIED_S6_S8_EPISODES_NOT_EXCHANGE_ACCOUNT_EQUITY_CHANGE",
            "LAST_CLOSE_DAY_ATTRIBUTION_NOT_CASHFLOW_DAY_OR_MARK_TO_MARKET",
            "OPEN_CARRY_IN_HAS_NO_ENTRY_TIME_LOOKBACK_LIMIT",
            "CURRENT_SETTLEMENT_REVISION_REQUIRED_LATER_FACTS_NOT_BACKDATED",
            "NET_INCLUDES_RECORDED_COSTS_NO_SECOND_FEE_SUBTRACTION",
            "FUNDING_NOT_SEPARATELY_LOADED_MISSING_COSTS_NOT_ZERO",
            "MAINTENANCE_EXCLUDED_NOT_TRADING_STRATEGY_PERFORMANCE",
            "RANKING_SELECTED_AFTER_THE_DAY_NOT_A_LIVE_SELECTION_RULE",
            "RANKING_COMPLEMENT_NOT_PROOF_SYMBOL_IS_OUTSIDE_TOP_MOVERS",
        ],
    }
    result["jev_participation"] = _empty_jev(result)
    result["execution_cost_evidence"] = empty_execution_cost_review()
    return result


def _empty_jev(result):
    return {
        "version": JEV_VERSION,
        "status": "UNKNOWN",
        "reason": "VERIFIED_SETTLEMENT_POPULATION_UNAVAILABLE",
        **{
            key: result[key]
            for key in (
                "scope_key",
                "account_environment",
                "window_start_ms",
                "window_end_ms",
                "as_of_ms",
            )
        },
        "basis": "FROZEN_ENTRY_EVIDENCE_FOR_VERIFIED_SETTLED_EPISODES",
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "denominator_settled_count": None,
        "denominator_net_usdt": None,
        "by_participation": {name: None for name in _JEV_GROUPS},
        "model_evaluated": None,
        "by_source": {},
        "fallback_causes": {},
        "coverage": {
            "query_complete": False,
            "queried_episode_count": 0,
            "evidence_row_count": 0,
            "classified_count": 0,
            "unknown_count": None,
            "retryable": False,
        },
        "limitations": [
            "ONLY_VERIFIED_SETTLED_TRADES_NOT_ALL_CANDIDATES_OR_MODEL_REQUESTS",
            "REJECTED_UNFILLED_CANDIDATES_OUTSIDE_THIS_DENOMINATOR",
            "FALLBACK_AND_DISABLED_ARE_NOT_MODEL_APPROVAL",
            "SHADOW_ONLY_IS_NOT_ADMISSION_OR_MODEL_EVALUATION_PROOF",
            "MISSING_OR_UNRECOGNIZED_ENTRY_EVIDENCE_IS_UNKNOWN",
            "GROUP_NET_COMPARISON_DOES_NOT_PROVE_MODEL_INCREMENTAL_PROFIT",
            "PRIMARY_SIGNAL_SOURCE_NOT_ALL_CONTRIBUTING_SIGNALS",
        ],
    }


def _read(conn, deadline, operation):
    remaining_ms = min(3000, int((deadline - time.monotonic()) * 1000))
    if remaining_ms <= 0:
        return None, "READ_BUDGET_EXHAUSTED"
    try:
        with conn.transaction():
            if conn.execute("SHOW transaction_read_only").fetchone() != ("on",):
                return None, "READ_ONLY_REQUIRED"
            if conn.execute("SHOW transaction_isolation").fetchone() not in {
                ("repeatable read",),
                ("serializable",),
            }:
                return None, "STABLE_SNAPSHOT_REQUIRED"
            # Integers only, never interpolated account/user content.
            conn.execute(f"SET LOCAL statement_timeout='{remaining_ms}ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            return operation(), None
    except Exception as exc:  # noqa: BLE001 - optional bounded research isolation
        return None, {
            "57014": "QUERY_TIMEOUT",
            "55P03": "QUERY_LOCK_TIMEOUT",
            "42501": "READ_PERMISSION_MISSING",
            "42P01": "READ_SCHEMA_UNAVAILABLE",
            "42703": "READ_SCHEMA_UNAVAILABLE",
        }.get(getattr(exc, "sqlstate", None), "READ_FAILED")


def _summary(trades):
    nets = [_number(row["net_usdt"]) for row in trades]
    gains = sum((value for value in nets if value > 0), Decimal(0))
    losses = -sum((value for value in nets if value < 0), Decimal(0))
    grosses = [_number(row["gross_usdt"]) for row in trades]
    fees = [
        _number(row["recorded_fees_usdt"])
        for row in trades
        if row["recorded_fees_usdt"] is not None
    ]
    wins = sum(value > 0 for value in nets)
    return {
        "settled_count": len(trades),
        "net_usdt": _text(sum(nets, Decimal(0))),
        "gross_usdt": _text(sum(grosses, Decimal(0))),
        "recorded_fees_usdt": _text(sum(fees, Decimal(0)))
        if len(fees) == len(trades)
        else None,
        "recorded_fees_known_count": len(fees),
        "recorded_fees_known_subtotal_usdt": _text(sum(fees, Decimal(0))),
        "funding_usdt": None,
        "funding_status": "NOT_SEPARATELY_LOADED",
        "wins": wins,
        "losses": sum(value < 0 for value in nets),
        "breakeven": sum(value == 0 for value in nets),
        "win_rate_pct": _text(
            (Decimal(wins) * 100 / len(trades)).quantize(Decimal("0.000001"))
        )
        if trades
        else None,
        "profit_factor": _text((gains / losses).quantize(Decimal("0.0000000001")))
        if losses
        else None,
        "profit_factor_status": "AVAILABLE" if losses else "NO_LOSS_DENOMINATOR",
        "gross_winning_net_usdt": _text(gains),
        "gross_losing_net_usdt": _text(losses),
        "held_minutes_median": _text(
            median(_number(row["held_minutes"]) for row in trades)
        )
        if trades
        else None,
    }


def _normalize(row, label, *, start, end, cutoff, scope_key):
    (
        identity,
        symbol,
        side,
        decided,
        oq,
        cq,
        onot,
        cnot,
        fees,
        opened,
        closed,
        fills_valid,
        all_known,
        terminal,
        orders_known,
        maintenance,
    ) = row
    if maintenance is True:
        raise ValueError("MAINTENANCE_EXCLUDED")
    if all_known is not True or orders_known is not True:
        raise ValueError("LATER_FILL_OR_ORDER_STATE")
    if fills_valid is not True:
        raise ValueError("INCONSISTENT_FILL_PATH")
    opening, closing = _number(oq), _number(cq)
    if opening <= 0 or closing < 0 or closing > opening:
        raise ValueError("INCONSISTENT_FILLS")
    if opening > closing:
        raise ValueError("OPEN_OR_PARTIAL_EXIT")
    if not all(_stamp(value) for value in (opened, closed, decided)) or not (
        decided <= opened <= closed <= cutoff
    ):
        raise ValueError("INVALID_FILL_TIMING")
    if not start <= closed < end:
        raise ValueError("NOT_CLOSED_IN_REVIEW_DAY")
    if terminal is not True:
        raise ValueError("AWAITING_TERMINAL_ORDERS")
    if not isinstance(label, dict) or label.get("state") != "SETTLED":
        state = label.get("state") if isinstance(label, dict) else None
        if state == "OPEN_OR_PARTIAL_EXIT":
            raise ValueError("SETTLEMENT_FILL_MISMATCH")
        raise ValueError(state if state in _LABEL_STATES else "LABEL_UNAVAILABLE")
    settled, revision = label.get("settled_ms"), label.get("settlement_revision")
    if (
        not _stamp(settled)
        or not closed <= settled <= cutoff
        or label.get("closed_ms") != closed
        or label.get("intent_id") != identity
        or type(revision) is not int
        or revision < 0
    ):
        raise ValueError("SETTLEMENT_BINDING_INVALID")
    if not is_supported_symbol(symbol, quote_assets=("USDT",)) or side not in {
        "BUY",
        "SELL",
    }:
        raise ValueError("TRADE_DIRECTION_OR_SYMBOL_INVALID")
    before, after, net = _number(onot), _number(cnot), _number(label.get("net_pnl"))
    if before <= 0 or after <= 0:
        raise ValueError("INVALID_ACCOUNTING_NUMBER")
    return {
        "trade_ref": _digest({"scope_key": scope_key, "episode": identity}),
        "symbol": symbol,
        "side": side,
        "opened_ms": opened,
        "closed_ms": closed,
        "settled_ms": settled,
        "settlement_revision": revision,
        "carry_in": opened < start,
        "held_minutes": _text(
            (Decimal(closed - opened) / 60000).quantize(Decimal("0.000001"))
        ),
        "net_usdt": _text(net),
        "gross_usdt": _text((after - before) * (1 if side == "BUY" else -1)),
        "recorded_fees_usdt": _text(_number(fees)) if fees is not None else None,
    }


def _jev_shadow_classification(projection):
    """A frozen observer receipt proves mode only, never model completion."""
    if not isinstance(projection, dict):
        return "UNKNOWN"
    if projection.get("shadow_present") is False:
        return None  # Existing gate classification retains its historical semantics.
    receipt, scope = projection.get("receipt"), projection.get("consumer_scope")
    if (
        projection.get("shadow_present") is not True
        or projection.get("gate_present") is not False
        or not isinstance(receipt, dict)
        or not isinstance(scope, list)
        or len(scope) != 5
        or not all(type(value) is str for value in scope)
        or scope[0] != "BINANCE"
        or scope[2:4] != ["SANDBOX", "FUTURES"]
        or scope[4] not in {"s6", "s8"}
        or receipt.get("schema") != "jev-shadow-receipt-v1"
        or type(receipt.get("status")) is not str
        or receipt["status"] not in _JEV_SHADOW_STATUSES
        or receipt.get("decision_effect") != "NONE"
        or any(
            receipt.get(key) is not False
            for key in ("execution_authorized", "risk_override", "admission_authorized")
        )
        or receipt.get("consumer") != json.dumps(scope, separators=(",", ":"))
        or type(receipt.get("candidate_digest")) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", receipt["candidate_digest"])
        or receipt.get("coverage")
        != (
            "MISSING_UNTIL_DURABLE_RESULT"
            if receipt["status"] == "SUBMITTED_NOT_DURABLE"
            else "MISSING"
        )
    ):
        return "UNKNOWN"
    signal = projection.get("signal_id")
    try:
        if type(signal) is not str or str(UUID(signal)) != signal:
            return "UNKNOWN"
    except ValueError:
        return "UNKNOWN"
    return "SHADOW_ONLY" if receipt.get("signal_id") == signal else "UNKNOWN"


def _jev_classification(row):
    if len(row) == 14:
        category = _jev_shadow_classification(row[13])
        if category is not None:
            return (
                category
                if row[1] is True and all(value is None for value in row[3:13])
                else "UNKNOWN"
            )
    (
        _identity,
        present,
        _source,
        schema,
        status,
        reason,
        cause,
        enabled,
        unavailable_action,
        trend,
        chase,
        minimum,
        maximum,
    ) = row[:13]
    if present is not True or schema not in {
        "jev-entry-gate-v1",
        "jev-fixed-entry-check-v1",
    }:
        return "UNKNOWN"
    if status == "SKIP" and reason in _JEV_SKIP_REASONS:
        return "NOT_MODEL_EVALUATED"
    if schema != "jev-entry-gate-v1":
        return "UNKNOWN"
    if status == "VETO" and reason == "PREEXISTING_REJECTION":
        return "NOT_MODEL_EVALUATED"
    if status == "PASS" and reason == "JEV_GATE_DISABLED" and enabled is False:
        return "DISABLED"
    if enabled is not True:
        return "UNKNOWN"
    if (
        status == "FALLBACK"
        and reason == "JEV_BASELINE_FALLBACK"
        and unavailable_action == "BASELINE"
        and cause in BASELINE_FALLBACK_CAUSES
    ):
        return "FALLBACK"
    if status == "PENDING" and reason == "JEV_RESULT_PENDING":
        return "NOT_MODEL_EVALUATED"
    # Recheck the frozen model scores and policy, never against today's TTL or
    # policy. PASS also describes a disabled gate, so status alone is not proof.
    try:
        trend, chase, minimum, maximum = (
            Decimal(_jev_decimal(value, "0", "1"))
            for value in (trend, chase, minimum, maximum)
        )
    except ValueError:
        return "UNKNOWN"
    if status == "PASS" and reason == "JEV_SECOND_FILTER_PASSED":
        return "MODEL_APPROVED" if trend >= minimum and chase <= maximum else "UNKNOWN"
    if status == "VETO" and (
        (reason == "JEV_TREND_UNSUPPORTED" and trend < minimum)
        or (reason == "JEV_CHASE_RISK_HIGH" and trend >= minimum and chase > maximum)
    ):
        return "MODEL_REJECTED"
    return "UNKNOWN"


def _jev_summary(trades):
    return {
        "settled_count": len(trades),
        "net_usdt": _text(
            sum((_number(row["net_usdt"]) for row in trades), Decimal(0))
        ),
    }


def _load_jev_participation(conn, deadline, scope_values, result, verified):
    jev = result["jev_participation"]
    if result["totals"] is None:
        return
    identities = sorted(verified)
    jev.update(
        denominator_settled_count=len(identities),
        denominator_net_usdt=result["totals"]["net_usdt"],
    )
    rows, error = [], None
    if identities:
        rows, error = _read(
            conn,
            deadline,
            lambda: conn.execute(
                _JEV_SQL,
                (result["as_of_ms"], identities, *scope_values, result["as_of_ms"]),
            ).fetchall(),
        )
    records = {}
    if not error:
        try:
            if len(rows) > len(identities) or any(
                len(row) not in {13, 14} for row in rows
            ):
                raise ValueError("INVALID_PROJECTION")
            records = {row[0]: row for row in rows}
            if len(records) != len(rows) or not set(records).issubset(verified):
                raise ValueError("INVALID_PROJECTION")
        except (ValueError, TypeError, IndexError):
            error = "JEV_ENTRY_EVIDENCE_DATA_INVALID"
            records = {}
    groups = {name: [] for name in _JEV_GROUPS}
    sources, causes = {}, {}
    for identity, trade in verified.items():
        record = records.get(identity)
        category = _jev_classification(record) if record else "UNKNOWN"
        groups[category].append(trade)
        source = record[2] if record and record[1] is True else None
        source = source if source in {"s2", "s3", "tv_bridge", "quant"} else "UNKNOWN"
        sources.setdefault(source, []).append(trade)
        if category == "FALLBACK":
            cause = record[6]
            cause = cause if cause in BASELINE_FALLBACK_CAUSES else "UNKNOWN"
            causes.setdefault(cause, []).append(trade)
    unknown_count = len(groups["UNKNOWN"])
    jev.update(
        status="PARTIAL"
        if unknown_count and unknown_count < len(identities)
        else "UNKNOWN"
        if unknown_count or error
        else "AVAILABLE",
        reason=error or ("JEV_ENTRY_EVIDENCE_INCOMPLETE" if unknown_count else None),
        by_participation={name: _jev_summary(rows) for name, rows in groups.items()},
        model_evaluated=_jev_summary(
            groups["MODEL_APPROVED"] + groups["MODEL_REJECTED"]
        ),
        by_source={name: _jev_summary(sources[name]) for name in sorted(sources)},
        fallback_causes={name: _jev_summary(causes[name]) for name in sorted(causes)},
    )
    jev["coverage"].update(
        query_complete=error is None,
        queried_episode_count=len(identities),
        evidence_row_count=sum(row[1] is True for row in records.values()),
        classified_count=len(identities) - unknown_count,
        unknown_count=unknown_count,
        retryable=bool(error) and error not in _NONRETRYABLE_READ_ERRORS,
    )


def _ranking(result, report):
    split = result["ranking_vs_other"]
    if result["status"] != "AVAILABLE":
        split["reason"] = "FULL_ACCOUNT_QUERY_UNPROVEN"
        return
    if not isinstance(report, dict) or (
        report.get("scope_key") != result["scope_key"]
        or report.get("account_environment") != result["account_environment"]
        or report.get("as_of_ms") != result["as_of_ms"]
        or not isinstance(report.get("window"), dict)
        or report["window"].get("start_ms") != result["window_start_ms"]
        or report["window"].get("end_ms") != result["window_end_ms"]
        or report["window"].get("timezone") != "Asia/Shanghai"
    ):
        split["reason"] = "RANKING_SCOPE_OR_CUTOFF_MISMATCH"
        return
    provided = report.get("execution_review")
    if not isinstance(provided, dict):
        return
    # Rebuild the bounded pure projection rather than trusting a supplied hash
    # or the mere presence of a symbol among the market's after-the-fact winners.
    verified = build_execution_review(report)
    if (
        provided != verified
        or verified.get("status") not in {"AVAILABLE", "PARTIAL"}
        or not isinstance(verified.get("trades"), list)
        or len(verified["trades"]) > MAX_TRADES
    ):
        split["reason"] = "RANKING_PROJECTION_UNVERIFIED"
        return
    current = {row["trade_ref"]: row for row in result["trades"]}
    refs = set()
    for row in verified["trades"]:
        ref = row.get("trade_ref")
        if (
            ref not in current
            or ref in refs
            or any(
                row.get(key) != current[ref][key]
                for key in ("symbol", "side", "opened_ms", "closed_ms", "settled_ms")
            )
        ):
            split["reason"] = "RANKING_SETTLEMENT_MISMATCH"
            return
        try:
            matches = _number(row.get("net_usdt")) == _number(current[ref]["net_usdt"])
        except ValueError:
            matches = False
        if not matches:
            split["reason"] = "RANKING_SETTLEMENT_MISMATCH"
            return
        refs.add(ref)
    split.update(
        status="AVAILABLE",
        reason=None,
        source_review_digest=verified["content_digest"],
        source_review_status=verified["status"],
        ranking=_summary([row for row in result["trades"] if row["trade_ref"] in refs]),
        other=_summary(
            [row for row in result["trades"] if row["trade_ref"] not in refs]
        ),
        other_meaning="NOT_IN_VERIFIED_RANKING_EPISODE_SAMPLE_NOT_NECESSARILY_OFF_RANKING_SYMBOLS",
    )


def _load_execution_costs(conn, deadline, scope_values, result, verified, details):
    costs = result["execution_cost_evidence"]
    if result["totals"] is None:
        return
    identities = sorted(verified)
    costs["denominator_settled_count"] = len(identities)
    rows, error = [], None
    if identities:
        rows, error = _read(
            conn,
            deadline,
            lambda: conn.execute(
                _COST_SQL,
                (
                    result["as_of_ms"],
                    result["as_of_ms"],
                    identities,
                    *scope_values,
                    result["as_of_ms"],
                ),
            ).fetchall(),
        )
    records = {}
    if not error:
        try:
            if len(rows) > len(identities) or any(
                len(row) != 2 or not isinstance(row[1], dict) for row in rows
            ):
                raise ValueError("INVALID_COST_PROJECTION")
            records = dict(rows)
            if len(records) != len(rows) or not set(records).issubset(verified):
                raise ValueError("INVALID_COST_PROJECTION")
        except (ValueError, TypeError, IndexError):
            error = "COST_EVIDENCE_DATA_INVALID"
            records = {}
    accounting = {
        row[0]: dict(
            zip(
                (
                    "opening_quantity",
                    "closing_quantity",
                    "opening_notional",
                    "closing_notional",
                ),
                row[4:8],
                strict=True,
            )
        )
        for row in details
        if row[0] in verified
    }
    cases = [
        build_execution_cost_case(trade, accounting[identity], records.get(identity))
        for identity, trade in verified.items()
    ]
    cases.sort(key=lambda row: row["trade_ref"])
    comparable = sum(row["status"] == "AVAILABLE" for row in cases)
    unknown = len(cases) - comparable
    costs.update(
        status="PARTIAL"
        if unknown and comparable
        else "UNKNOWN"
        if unknown or error
        else "AVAILABLE",
        reason=error or ("FROZEN_PLAN_EVIDENCE_INCOMPLETE" if unknown else None),
        plan_comparable_count=comparable,
        native_stop_decomposed_count=sum(
            row["stop_decomposition"]["status"] == "AVAILABLE" for row in cases
        ),
        cases=cases,
    )
    costs["coverage"].update(
        query_complete=error is None,
        queried_episode_count=len(identities),
        evidence_row_count=len(records),
        unknown_count=unknown,
        retryable=bool(error) and error not in _NONRETRYABLE_READ_ERRORS,
    )


def load_account_day_review(conn, scope, *, start_ms, end_ms, as_of_ms, report=None):
    """Inspect <=500 closed-episode candidates in <=20 seconds, never trade.

    Caller owns a repeatable-read read-only transaction. Each query has a nested
    savepoint, a <=3-second statement deadline and 100-ms lock deadline. Errors
    are fixed codes, not exception strings. Queries never contact a venue.
    """
    result = _empty(scope, start_ms, end_ms, as_of_ms)
    if (
        not isinstance(scope, AccountScope)
        or not all(_stamp(value) for value in (start_ms, end_ms, as_of_ms))
        or end_ms - start_ms != DAY_MS
        or (start_ms + _OFFSET_MS) % DAY_MS
        or not end_ms <= as_of_ms
    ):
        result["reason"] = "ACCOUNT_REVIEW_INPUT_INVALID"
        return _seal(result)
    deadline = time.monotonic() + MAX_READ_SECONDS
    scope_values = tuple(asdict(scope).values())
    candidates, error = _read(
        conn,
        deadline,
        lambda: conn.execute(
            _CANDIDATES_SQL,
            (*scope_values, as_of_ms, start_ms, end_ms, as_of_ms),
        ).fetchall(),
    )
    if error:
        result["reason"] = error
        result["coverage"]["retryable"] = error not in _NONRETRYABLE_READ_ERRORS
        return _seal(result)
    result["coverage"]["candidate_count"] = len(candidates)
    if len(candidates) > MAX_TRADES:
        result["reason"] = "ACCOUNT_CANDIDATE_LIMIT"
        result["coverage"]["truncated"] = True
        return _seal(result)
    try:
        ids = [str(UUID(row[0])) for row in candidates]
        if len(ids) != len(set(ids)) or any(len(row) != 1 for row in candidates):
            raise ValueError("INVALID_CANDIDATES")
    except (ValueError, TypeError, AttributeError, IndexError):
        result["reason"] = "ACCOUNT_CANDIDATE_DATA_INVALID"
        return _seal(result)
    details = []
    if ids:
        details, error = _read(
            conn,
            deadline,
            lambda: conn.execute(
                _DETAILS_SQL,
                (as_of_ms, as_of_ms, as_of_ms, ids, *scope_values),
            ).fetchall(),
        )
        if error:
            result["reason"] = error
            result["coverage"]["retryable"] = error not in _NONRETRYABLE_READ_ERRORS
            return _seal(result)
    try:
        if (
            len(details) != len(ids)
            or {row[0] for row in details} != set(ids)
            or any(len(row) != 16 for row in details)
        ):
            raise ValueError("INCOMPLETE_DETAILS")
    except (TypeError, ValueError, IndexError):
        result["reason"] = "ACCOUNT_DETAIL_COVERAGE_INVALID"
        return _seal(result)
    labels, errors = {}, {}
    for offset in range(0, len(ids), 100):
        batch = ids[offset : offset + 100]
        loaded, error = _read(
            conn,
            deadline,
            lambda batch=batch: outcomes(conn, scope, batch, now=as_of_ms),
        )
        if error or not isinstance(loaded, dict):
            errors.update(
                {identity: error or "LABEL_DATA_INVALID" for identity in batch}
            )
        else:
            labels.update({identity: loaded.get(identity) for identity in batch})
    unknown, trades, verified = Counter(), [], {}
    with localcontext() as context:
        context.prec = 100
        for row in details:
            if row[0] in errors:
                unknown[errors[row[0]]] += 1
                continue
            try:
                trade = _normalize(
                    row,
                    labels.get(row[0]),
                    start=start_ms,
                    end=end_ms,
                    cutoff=as_of_ms,
                    scope_key=result["scope_key"],
                )
                trades.append(trade)
                verified[row[0]] = trade
            except ValueError as exc:
                # All normalization errors above are constant code strings.
                unknown[str(exc)] += 1
            except TypeError:
                unknown["ACCOUNT_DETAIL_DATA_INVALID"] += 1
        incomplete = bool(errors) or any(
            key not in _DEFINITE_EXCLUSIONS for key in unknown
        )
        result.update(
            status="PARTIAL"
            if incomplete and trades
            else "UNKNOWN"
            if incomplete
            else "AVAILABLE",
            reason="ACCOUNT_SETTLEMENT_COVERAGE_INCOMPLETE" if incomplete else None,
            trades=sorted(trades, key=lambda row: (row["closed_ms"], row["trade_ref"])),
            totals=_summary(trades) if trades or not incomplete else None,
            by_side={
                side: _summary([row for row in trades if row["side"] == side])
                for side in ("BUY", "SELL")
            }
            if trades or not incomplete
            else {},
        )
        result["coverage"].update(
            query_complete=not errors,
            retryable=bool(errors) or bool(set(unknown) & _RETRYABLE_SETTLEMENT_STATES),
            verified_settled_count=len(trades),
            excluded_reasons=dict(sorted(unknown.items())),
            full_closed_settlement_population_proven=not incomplete,
            carry_in_settled_count=sum(row["carry_in"] for row in trades),
        )
        try:
            _ranking(result, report)
        except Exception:  # noqa: BLE001 - bad optional rankings cannot lose account facts
            result["ranking_vs_other"]["reason"] = "RANKING_REVIEW_INVALID"
        try:
            _load_jev_participation(conn, deadline, scope_values, result, verified)
        except Exception:  # noqa: BLE001 - optional evidence cannot lose account facts
            result["jev_participation"] = _empty_jev(result)
            result["jev_participation"]["reason"] = "JEV_ENTRY_EVIDENCE_DATA_INVALID"
        try:
            _load_execution_costs(
                conn, deadline, scope_values, result, verified, details
            )
        except Exception:  # noqa: BLE001 - optional costs cannot lose account facts
            result["execution_cost_evidence"] = empty_execution_cost_review()
            result["execution_cost_evidence"]["reason"] = "COST_EVIDENCE_DATA_INVALID"
    return _seal(result)
