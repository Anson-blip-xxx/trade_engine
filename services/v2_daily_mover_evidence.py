"""Bounded factual daily-mover review, never an admission or execution port.

Signals are shared within an environment. Decisions and receipts belong to an
exact account consumer; a shared signal is NOT proof that this account evaluated
it. Missing history, expiry, JEV unavailability and a model veto remain distinct.
The caller owns a short read-only snapshot and closes it before market I/O.
"""

import re
import time
from collections import Counter
from dataclasses import asdict
from decimal import Decimal, InvalidOperation, localcontext

from services.v2_reentry_evaluation import outcomes
from services.v2_trade_review import recorded_exit_reason
from v2_core.account_risk import AccountScope
from v2_core.directional import supports_event
from v2_core.strategy import StrategyScope
from v2_core.symbols import is_supported_symbol

DAY_MS = 86400000
OFFSET_MS = 28800000
SCHEMA = "daily-mover-account-evidence-v1"
SIGNAL_WINDOWS = 4
SIGNAL_WINDOW_MS = DAY_MS // SIGNAL_WINDOWS
CLASSIFICATION_VERSION = "directional-entry-events-v2"
SIGNAL_SAMPLE_BASIS = "SOURCE_FAIR_FOUR_UTC8_WINDOWS_UNIFORM_RANK"
_SOURCES = ("s3", "tv_bridge", "quant")
_CONTEXT_REASONS = frozenset(
    {"CANDIDATE_DATA_UNAVAILABLE_AFTER_RETRY", "JEV_GATE_UNAVAILABLE"}
)
_NO_CANDIDATE = frozenset(
    {
        "UNSUPPORTED_DIRECTIONAL_EVENT",
        "producer lifecycle ended",
        "TV_BEFORE_ENABLE_TIME",
        "TV_SYMBOL_OUTSIDE_MARKET_UNIVERSE",
        "QUANT_ACCOUNT_DISABLED",
    }
)

# Paths are an explicit export allowlist, not a recursive snapshot/config copy.
# Frozen source-specific thresholds take precedence over the base policy below.
_OPERAND_PATHS = {
    "strength": "features,signal,strength",
    "score": "features,evaluation,market_plan,score",
    "price": "features,context,directional,price",
    "regime": "features,context,directional,regime",
    "short_ratio": "features,context,directional,short_ratio",
    "funding_rate": "features,context,directional,funding_rate",
    "expected_move_pct": "features,context,directional,expected_move_pct",
    "event_taker_buy_ratio": "features,signal,taker_buy_ratio",
    "breakout_confirmed": "features,signal,breakout_confirmed",
    **{
        f"{window}.{field}": f"features,context,directional,market,{window},{field}"
        for window, fields in {
            "15m": ("ema20", "atr", "rsi", "taker_buy_ratio"),
            "1h": ("ema20", "atr", "atr_pct"),
            "4h": ("ema20", "chg"),
            "24h": ("ema20", "ema60", "chg"),
        }.items()
        for field in fields
    },
    **{
        key: f"features,evaluation,source_scoring,structure,values,{key}"
        for key in (
            "open",
            "close",
            "previous_open",
            "previous_close",
            "previous_high",
            "previous_low",
            "ema20",
            "ema60",
            "ema20_slope",
            "ema60_slope",
            "atr",
            "atr_pct",
            "taker_buy_ratio",
            "volume_ratio",
            "prior20_high",
            "prior20_low",
            "prior3_high",
            "prior3_low",
            "breakout_long",
            "breakout_short",
            "prior3_touched_ema20_long",
            "prior3_touched_ema20_short",
        )
    },
    **{
        key: f"features,evaluation,source_scoring,{key}"
        for key in (
            "raw_strength",
            "reference_price",
            "entry_anchor",
            "target_price",
            "structural_stop_price",
            "planned_net_reward_risk",
            "cost_buffer_fraction",
        )
    },
    "structure_status": "features,evaluation,source_scoring,structure,status",
    "structure_last_closed_at_ms": "features,evaluation,source_scoring,structure,last_closed_at_ms",
    "assembled_at_ms": "features,context,assembled_at",
    "stop_fraction": "features,evaluation,market_plan,stop_fraction",
    "analysis_factor": "features,evaluation,analysis,factor",
    **{
        f"sizing.{key}": f"features,context,directional,sizing,{key}"
        for key in (
            "balance",
            "available_margin",
            "used_pool_margin",
            "min_quantity",
            "min_notional",
            "quantity_step",
            "price_tick",
            "remaining_portfolio_risk",
            "sizing_mark_price",
            "price_drift_fraction",
            "drawdown_factor",
        )
    },
    **{
        f"sizing.{key}": f"features,evaluation,sizing_explanation,{key}"
        for key in (
            "reference_price",
            "mark_price",
            "price_high",
            "price_low",
            "unit_stop_risk",
            "stop_valid_through_envelope",
            "stop_price",
            "rounded_quantity",
            "budgeted_stop_risk",
        )
    },
    **{
        f"liquidity.{key}": f"features,evaluation,liquidity_gate,observed,{key}"
        for key in (
            "age_ms",
            "spread_bps",
            "price_basis_bps",
            "vwap_impact_bps",
            "worst_impact_bps",
        )
    },
    **{
        f"jev.{key}": f"features,evaluation,jev_entry_gate,timing,{key}"
        for key in (
            "deadline_ms",
            "checked_at_ms",
            "started_at_ms",
            "signal_expires_at_ms",
        )
    },
    **{
        f"jev.{key}": f"features,evaluation,jev_entry_gate,hypotheses,{key}"
        for key in ("trend_supported", "chase_risk", "liquidity_fragile")
    },
    "jev.model_expires_at_ms": "features,evaluation,jev_entry_gate,model_expires_at_ms",
}
_POLICY_KEYS = (
    "entry.min_score",
    "entry.short_min_strength",
    "entry.flow_long",
    "entry.flow_short",
    "entry.reversal_rsi_long",
    "entry.reversal_rsi_short",
    "entry.extension_default",
    "entry.extension_violent",
    "entry.max_atr",
    "entry.max_takeover_atr",
    "entry.takeover_chg_4h",
    "entry.takeover_chg_24h",
    "entry.takeover_pullback_atr",
    "entry.takeover_pullback_fraction",
    "entry.pump_down_guard_chg",
    "entry.min_reward_risk",
    "entry.max_adverse_funding",
    "entry.confirmation_version",
    "trend.max_structure_age_ms",
    "trend.max_atr_pct",
    "trend.min_volume_ratio",
    "trend.max_chase_atr",
    "trend.stop_buffer_atr",
    "trend.stop_atr",
    "trend.min_stop_fraction",
    "trend.max_stop_fraction",
    "trend.target_atr",
    "stop.max_default",
    "sizing.cost_buffer_fraction",
    "sizing.risk_fraction",
    "sizing.pool_fraction",
    "sizing.min_allocation",
    "sizing.max_allocation",
)
_SOURCE_PATHS = {
    "market_environment": "features,context,directional,market,input_evidence,environment",
    "market_source": "features,context,directional,market,input_evidence,source",
    "market_host": "features,context,directional,market,input_evidence,source_host",
    "market_window_version": "features,context,directional,market,input_evidence,window_version",
    "market_closed_at_ms": "features,context,directional,market,input_evidence,closed_at",
    "s3_environment": "features,context,sources,s3,environment",
    "s3_source": "features,context,sources,s3,source",
    "s3_observed_at_ms": "features,context,sources,s3,observed_at",
    "structure_environment": "features,evaluation,source_scoring,structure,environment",
    "structure_source": "features,evaluation,source_scoring,structure,source",
    "liquidity_environment": "features,evaluation,liquidity_gate,observed,source_environment",
    "liquidity_host": "features,evaluation,liquidity_gate,observed,source_host",
}
_FROZEN_THRESHOLD_PATHS = {
    "entry.min_score": "features,evaluation,source_scoring,min_score",
    "entry.short_min_strength": "features,evaluation,source_scoring,short_min_strength",
    "jev.min_trend_support": "features,evaluation,jev_entry_gate,policy,min_trend_support",
    "jev.max_chase_risk": "features,evaluation,jev_entry_gate,policy,max_chase_risk",
    **{
        f"jev.{key}": f"features,evaluation,jev_entry_gate,policy,{key}"
        for key in ("max_wait_ms", "max_age_ms", "cache_ttl_ms", "unavailable_wait_ms")
    },
    **{
        f"liquidity.{key}": f"features,context,liquidity_controls,values,{key}"
        for key in (
            "max_age_ms",
            "max_spread_bps",
            "max_impact_bps",
            "max_price_basis_bps",
        )
    },
}


def _projection_sql(column, paths):
    # PostgreSQL functions accept at most 100 arguments. Each chunk has <=60.
    pairs = [f"'{key}',{column}#>'{{{path}}}'" for key, path in paths.items()]
    return " || ".join(
        "jsonb_build_object(" + ",".join(pairs[i : i + 30]) + ")"
        for i in range(0, len(pairs), 30)
    )


_DETAIL_SQL = (
    "jsonb_build_object('operands',"
    + _projection_sql("e.snapshot", _OPERAND_PATHS)
    + ",'thresholds',"
    + _projection_sql("e.config", {key: f"policy,{key}" for key in _POLICY_KEYS})
    + ", 'source_thresholds',"
    + _projection_sql("e.snapshot", _FROZEN_THRESHOLD_PATHS)
    + ",'market_sources',"
    + _projection_sql("e.snapshot", _SOURCE_PATHS)
    + ")"
)


def _public_symbol(value):
    return (
        isinstance(value, str)
        and 5 <= len(value) <= 44
        and value.endswith("USDT")
        and all(c.isalnum() or c == "_" for c in value)
    )


# Explicit projections: never return raw snapshots, configs, model prompts,
# request payloads or credential-bearing exception details.
_SIGNALS_SQL = """WITH source_windows AS MATERIALIZED (
  SELECT source,cap FROM unnest(%s::text[],%s::integer[]) x(source,cap) WHERE cap>0),
base AS MATERIALIZED (
  SELECT s.signal_id,s.source,s.snapshot->>'signal' signal,
    (s.snapshot->>'observed_at')::bigint observed_ms,s.received_at,w.cap,
    (s.snapshot->>'expires_at_ms')::bigint expires_ms,
    (((s.snapshot->>'observed_at')::bigint+28800000) %% 86400000)/21600000 bucket
  FROM v2_inbound_signals s JOIN source_windows w ON s.source=w.source
  WHERE environment=%s
    AND ((snapshot->>'expires_at_ms')::bigint)>%s
    AND (snapshot->>'observed_at')::bigint >= %s
    AND (snapshot->>'observed_at')::bigint < %s
    AND snapshot->>'symbol'=%s
    AND received_at<=to_timestamp(%s/1000.0)
),ranked AS (
  SELECT *,row_number() OVER(PARTITION BY source,bucket ORDER BY observed_ms,signal_id) rn,
    count(*) OVER(PARTITION BY source,bucket) population,
    cap/4+(bucket<cap%%4)::integer sample_cap FROM base),
marked AS (
  SELECT *,CASE WHEN population<=sample_cap THEN true
    WHEN sample_cap=0 THEN false
    WHEN sample_cap=1 THEN rn=(population+1)/2
    ELSE rn=1 OR (rn-1)*(sample_cap-1)/(population-1)>
                  (rn-2)*(sample_cap-1)/(population-1) END sampled
  FROM ranked),
chosen AS MATERIALIZED (
  SELECT * FROM marked WHERE sampled OR (population>sample_cap AND rn=
    CASE WHEN sample_cap=0 OR (sample_cap=1 AND (population+1)/2<>1)
         THEN 1 ELSE 2 END))
SELECT s.signal_id::text,s.source,s.signal,s.observed_ms,
  (extract(epoch FROM s.received_at)*1000)::bigint,
  c.consumer,d.decision_id::text,d.action,d.side,d.evidence_ref,
  (extract(epoch FROM d.created_at)*1000)::bigint,
  e.snapshot->>'rationale',e.config_digest,e.strategy_version,
  jsonb_build_object(
    'stage',e.snapshot->'features'->'evaluation'->>'stage',
    'market_reason',e.snapshot->'features'->'evaluation'->'market_plan'->>'reason',
    'sizing_reason',e.snapshot->'features'->'evaluation'->'sizing'->>'reason',
    'analysis_reason',e.snapshot->'features'->'evaluation'->'analysis'->>'reason',
    'execution_gate',e.snapshot->'features'->'evaluation'->>'execution_market_gate',
    'liquidity_reason',e.snapshot->'features'->'evaluation'->'liquidity_gate'->>'reason',
    'jev_status',e.snapshot->'features'->'evaluation'->'jev_entry_gate'->>'status',
    'jev_reason',e.snapshot->'features'->'evaluation'->'jev_entry_gate'->>'reason',
    'jev_fallback',e.snapshot->'features'->'evaluation'->'jev_entry_gate'->>'fallback_cause',
    'context_cause',e.snapshot->'features'->'evaluation'->'candidate_context_resolution'->>'cause',
    'maintenance_origin',e.snapshot#>>'{features,evaluation,maintenance_origin}'),
  r.outcome,r.reason,r.intent_id::text,
  (extract(epoch FROM r.completed_at)*1000)::bigint,
  s.expires_ms,s.sampled,DETAIL_PROJECTION
FROM chosen s CROSS JOIN unnest(%s::text[]) c(consumer)
LEFT JOIN v2_strategy_decisions d ON d.signal_id=s.signal_id AND d.consumer=c.consumer
  AND d.created_at<=to_timestamp(%s/1000.0)
LEFT JOIN v2_decision_evidence e ON e.evidence_ref=d.evidence_ref
  AND e.created_at<=to_timestamp(%s/1000.0)
LEFT JOIN v2_signal_receipts r ON r.signal_id=s.signal_id AND r.consumer=c.consumer
  AND r.completed_at<=to_timestamp(%s/1000.0)
ORDER BY s.observed_ms,s.signal_id,c.consumer""".replace(
    "DETAIL_PROJECTION", _DETAIL_SQL
)

_INTENTS_SQL = """WITH selected AS MATERIALIZED (
  SELECT i.* FROM v2_trade_intents i
  WHERE (exchange,account_id,environment,product)=(%s,%s,%s,%s)
    AND producer IN ('s6','s8') AND payload->>'symbol'=%s
    AND created_at>=to_timestamp(%s/1000.0)
    AND created_at<to_timestamp(%s/1000.0)
    AND created_at<=to_timestamp(%s/1000.0)
  ORDER BY created_at DESC,intent_id LIMIT %s)
SELECT i.intent_id::text,i.signal_id::text,i.evidence_ref,
  i.payload->>'symbol',i.payload->>'side',i.producer,
  (extract(epoch FROM i.created_at)*1000)::bigint,
  e.snapshot->>'source',e.snapshot->'features'->'evaluation'->'market_plan'->>'entry_mode',
  e.config_digest,e.strategy_version,
  f.oq::text,f.cq::text,f.onot::text,f.cnot::text,f.fees::text,
  f.opened_ms,f.closed_ms,f.close_orders,f.fill_count,
  term.event_id::text,term.payload->>'status',term.payload->>'reason',
  (extract(epoch FROM term.created_at)*1000)::bigint,
  cl.order_id::text,cl.request_evidence->>'reason',
  cl.request_evidence->>'origin',cl.request_evidence->>'kind',
  coalesce(ord.items,'[]'::jsonb),
  e.snapshot->'features'->'evaluation'->'market_plan'->>'stop_fraction',
  e.snapshot->'features'->'context'->'directional'->>'price'
FROM selected i
JOIN v2_decision_evidence e ON e.evidence_ref=i.evidence_ref
  AND e.created_at<=to_timestamp(%s/1000.0)
LEFT JOIN LATERAL (
  SELECT coalesce(sum(z.quantity) FILTER(WHERE o.leg='OPEN'),0) oq,
    coalesce(sum(z.quantity) FILTER(WHERE o.leg='CLOSE'),0) cq,
    sum(z.quantity*z.price) FILTER(WHERE o.leg='OPEN') onot,
    sum(z.quantity*z.price) FILTER(WHERE o.leg='CLOSE') cnot,
    CASE WHEN bool_and(z.fee_currency='USDT') THEN sum(z.fee) END fees,
    min(z.occurred_at_ms) FILTER(WHERE o.leg='OPEN') opened_ms,
    max(z.occurred_at_ms) FILTER(WHERE o.leg='CLOSE') closed_ms,
    count(DISTINCT o.order_id) FILTER(WHERE o.leg='CLOSE') close_orders,
    count(*) fill_count
  FROM v2_orders o JOIN v2_fills z USING(order_id)
  WHERE o.episode_id=i.intent_id AND z.occurred_at_ms<=%s
    AND z.created_at<=to_timestamp(%s/1000.0)) f ON true
LEFT JOIN LATERAL (
  SELECT event_id,payload,created_at FROM v2_domain_outbox
  WHERE intent_id=i.intent_id AND event_type LIKE 'INTENT_TERMINAL:%%'
    AND created_at<=to_timestamp(%s/1000.0)
  ORDER BY created_at DESC,event_id DESC LIMIT 1) term ON true
LEFT JOIN LATERAL (
  SELECT o.order_id,o.request_evidence FROM v2_orders o
  JOIN v2_fills z USING(order_id)
  WHERE o.episode_id=i.intent_id AND o.leg='CLOSE'
    AND z.occurred_at_ms<=%s AND z.created_at<=to_timestamp(%s/1000.0)
  ORDER BY z.occurred_at_ms DESC,z.fill_key DESC LIMIT 1) cl ON true
LEFT JOIN LATERAL (
  SELECT jsonb_agg(jsonb_build_object('order_id',r.order_id,
    'leg',r.leg,'status',r.status,'reason',r.reason,
    'event_id',r.event_id,'known_ms',r.known_ms)) items
  FROM (
    SELECT o.order_id::text,o.leg,last_event.status,
      last_event.evidence->>'reason' reason,last_event.event_id::text,
      (extract(epoch FROM last_event.created_at)*1000)::bigint known_ms
    FROM v2_orders o
    JOIN LATERAL (SELECT status,event_id,evidence,created_at
      FROM v2_order_events oe WHERE oe.order_id=o.order_id
        AND oe.created_at<=to_timestamp(%s/1000.0)
      ORDER BY version DESC LIMIT 1) last_event ON true
    WHERE o.episode_id=i.intent_id ORDER BY o.order_id LIMIT 21) r) ord ON true
ORDER BY i.created_at DESC,i.intent_id"""


def _text(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_ .:/-]{1,160}", value
    ):
        return "UNRECOGNIZED_EVIDENCE_CODE"
    return value


def _stage(record):
    action, reason, fields = record["action"], record["reason"], record["fields"]
    if action == "EXPIRED" or record["receipt_outcome"] == "EXPIRED":
        return "EXPIRED_NOT_STRATEGY_REJECTION"
    if action == "OPEN":
        return "ENTRY_CANDIDATE"
    if action is None:
        if record["receipt_outcome"] is not None:
            return "RECEIPT_WITHOUT_DECISION_EVIDENCE"
        return "ACCOUNT_DECISION_NOT_OBSERVED"
    if not record["evidence_ref"] or reason is None:
        return "DECISION_EVIDENCE_UNAVAILABLE"
    # Veto is proven by the immutable filter assessment, not any JEV prefix.
    if fields.get("jev_status") == "VETO":
        return "JEV_VETO"
    if fields.get("jev_status") == "SKIP" or reason == "JEV_GATE_UNAVAILABLE":
        return "JEV_UNAVAILABLE_OR_SKIPPED"
    if reason in _CONTEXT_REASONS or fields.get("context_cause"):
        return "CANDIDATE_DATA_UNAVAILABLE"
    if reason in _NO_CANDIDATE:
        return "NO_ENTRY_CANDIDATE"
    if fields.get("stage") == "SIGNAL_ONLY_PREFILTER":
        return "SIGNAL_PREFILTER_REJECTED"
    if fields.get("market_reason") not in {None, "CANDIDATE"}:
        return "MARKET_FILTER_REJECTED"
    if fields.get("sizing_reason") not in {None, "SIZED"}:
        return "SIZING_RISK_REJECTED"
    if (
        fields.get("liquidity_reason") == reason
        or fields.get("execution_gate") == reason
    ):
        return "EXECUTION_MARKET_RISK_REJECTED"
    # Preserve reason without pretending every strategy gate is account risk.
    return "OTHER_RECORDED_REJECTION"


def _event_class(record):
    strategy = record.get("consumer_strategy", "").upper()
    event = record.get("event")
    if strategy not in {"S6", "S8"} or not isinstance(event, str):
        return "UNKNOWN_EVENT"
    if supports_event(strategy, event):
        return "DIRECTIONAL_ENTRY_EVENT"
    if supports_event("S8" if strategy == "S6" else "S6", event):
        return "OPPOSITE_DIRECTION_EVENT"
    if event in {"ATR_EXPAND", "HIGH_VOL", "LOW_VOL", "EVENT_END"}:
        return "NON_ENTRY_EVENT"
    return "UNKNOWN_EVENT"


def _lifecycle(record):
    expired = (
        record.get("action") == "EXPIRED" or record.get("receipt_outcome") == "EXPIRED"
    )
    if expired:
        origin = record.get("fields", {}).get("maintenance_origin")
        if origin in {"SIGNAL_MAINTENANCE", "BOUNDED_HISTORY_SWEEP"}:
            return "RECORDED_EXPIRY_MAINTENANCE"
        if not record.get("decision_id"):
            # A late receipt can also be a real scheduling delay. Neither its
            # wording nor its age proves that it came from a history sweep.
            return "EXPIRED_WITHOUT_DECISION_ORIGIN_UNKNOWN"
        return "RECORDED_EXPIRED_DECISION"
    decided, expires = record.get("decided_ms"), record.get("expires_ms")
    if record.get("action") is None:
        return "ACCOUNT_DECISION_NOT_OBSERVED"
    if type(decided) is int and type(expires) is int:
        return (
            "DECISION_BEFORE_EXPIRY" if decided < expires else "DECISION_AFTER_EXPIRY"
        )
    return "DECISION_TIMING_UNKNOWN"


def _representatives(records, limit):
    """Cover source/time/consumer cells, then reasons, then the remaining range."""
    ordered = sorted(
        records,
        key=lambda r: (
            r["observed_ms"],
            r["signal_id"],
            r.get("consumer_strategy", ""),
        ),
    )
    if len(ordered) <= limit:
        return ordered
    chosen = set()
    for key in (
        lambda r: (
            r["source"],
            (r["observed_ms"] + OFFSET_MS) % DAY_MS // SIGNAL_WINDOW_MS,
            r.get("consumer_strategy"),
        ),
        lambda r: (
            _event_class(r),
            _stage(r),
            r.get("reason"),
            r.get("receipt_reason"),
        ),
    ):
        groups = {}
        for i, record in enumerate(ordered):
            groups.setdefault(key(record), []).append(i)
        uncovered = [
            indices
            for indices in groups.values()
            if not any(i in chosen for i in indices)
        ]
        remaining_slots = limit - len(chosen)
        if len(uncovered) > remaining_slots:
            positions = (
                [len(uncovered) // 2]
                if remaining_slots == 1
                else [
                    i * (len(uncovered) - 1) // (remaining_slots - 1)
                    for i in range(remaining_slots)
                ]
            )
            uncovered = [uncovered[i] for i in positions]
        for indices in uncovered:
            chosen.add(indices[len(indices) // 2])
            if len(chosen) == limit:
                return [ordered[i] for i in sorted(chosen)]
    remaining = [i for i in range(len(ordered)) if i not in chosen]
    needed = limit - len(chosen)
    chosen.update(
        remaining[(2 * i + 1) * len(remaining) // (2 * needed)] for i in range(needed)
    )
    return [ordered[i] for i in sorted(chosen)]


def _safe_scalar(value, *, code=False):
    if type(value) is bool:
        return value
    if type(value) in {int, float, str}:
        raw = str(value)
        if len(raw) <= 80:
            try:
                number = Decimal(raw)
                if number.is_finite() and (
                    not number or -30 <= number.adjusted() <= 30
                ):
                    return value if type(value) in {int, float} else raw
            except InvalidOperation:
                pass
        if (
            code
            and isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,80}", value)
        ):
            return value
    return None


def _frozen_details(raw):
    raw = raw if isinstance(raw, dict) else {}
    output = {}
    for group, allowed, codes in (
        ("operands", _OPERAND_PATHS, {"regime", "structure_status"}),
        ("thresholds", (*_POLICY_KEYS, *_FROZEN_THRESHOLD_PATHS), set()),
        ("market_sources", _SOURCE_PATHS, set(_SOURCE_PATHS)),
    ):
        values = raw.get(group)
        values = values if isinstance(values, dict) else {}
        output[group] = {
            key: safe
            for key in allowed
            if (safe := _safe_scalar(values.get(key), code=key in codes)) is not None
        }
    overrides = raw.get("source_thresholds")
    if isinstance(overrides, dict):
        for key in _FROZEN_THRESHOLD_PATHS:
            value = _safe_scalar(overrides.get(key))
            if value is not None:
                output["thresholds"][key] = value
    return output


def _rejection_details(records):
    groups = {}
    for record in records:
        if (
            record.get("action") not in {None, "OPEN", "EXPIRED"}
            and record.get("reason")
            and _stage(record) != "EXPIRED_NOT_STRATEGY_REJECTION"
        ):
            groups.setdefault((_stage(record), record["reason"]), []).append(record)
    result, remaining = [], 50
    for (stage, reason), rows in sorted(
        groups.items(), key=lambda item: (-len(item[1]), item[0])
    ):
        samples = []
        for row in _representatives(rows, min(3, remaining)) if remaining else []:
            detail = {
                key: row.get(key)
                for key in (
                    "signal_id",
                    "source",
                    "event",
                    "observed_ms",
                    "received_ms",
                    "expires_ms",
                    "decided_ms",
                    "completed_ms",
                    "evidence_ref",
                    "strategy_version",
                    "config_digest",
                )
            }
            detail.update(_frozen_details(row.get("rejection_detail")))
            detail["evidence_basis"] = (
                "FROZEN_EXPLICIT_FIELDS_NO_CURRENT_POLICY_DEFAULTS"
            )
            detail["lifecycle"] = _lifecycle(row)
            detail["numeric_evidence_status"] = (
                "RECORDED_OPERANDS_AND_THRESHOLDS"
                if detail["operands"] and detail["thresholds"]
                else "PARTIAL_RECORDED_FIELDS"
                if detail["operands"] or detail["thresholds"]
                else "NOT_RECORDED_IN_PROJECTION"
            )
            samples.append(detail)
        remaining -= len(samples)
        result.append(
            {
                "stage": stage,
                "reason": reason,
                "count": len(rows),
                "reason_samples": samples,
            }
        )
    return result


def summarize_symbol_evidence(records, *, truncated=False):
    """Pure reduction; counts are consumer-decisions, sources count unique signals."""
    signals, sources, stages, reasons, jev, samples = (
        {},
        Counter(),
        Counter(),
        Counter(),
        Counter(),
        [],
    )
    for record in records:
        if record["signal_id"] not in signals:
            signals[record["signal_id"]] = record["observed_ms"]
            sources[record["source"]] += 1
        stage = _stage(record)
        stages[stage] += 1
        if record["action"] is not None and record["reason"] is not None:
            reasons[(stage, record["reason"])] += 1
        fields = record["fields"]
        if fields.get("jev_status"):
            jev[
                (
                    fields["jev_status"],
                    fields.get("jev_reason"),
                    fields.get("jev_fallback"),
                )
            ] += 1
        # Keep a deterministic bounded set of source references. Counts include
        # all examined rows; this is not a replacement for original PG evidence.
    samples = [
        {
            **record,
            "stage": _stage(record),
            "event_classification": _event_class(record),
            "lifecycle": _lifecycle(record),
        }
        for record in _representatives(records, 50)
    ]
    by_strategy = {}
    for producer in ("s6", "s8"):
        all_records = [r for r in records if r.get("consumer_strategy") == producer]
        selected = [
            r for r in all_records if _event_class(r) == "DIRECTIONAL_ENTRY_EVENT"
        ]
        excluded = [
            r for r in all_records if _event_class(r) != "DIRECTIONAL_ENTRY_EVENT"
        ]
        selected_reasons = Counter(
            (_stage(r), r["reason"])
            for r in selected
            if r["action"] is not None and r["reason"] is not None
        )
        selected_jev = Counter(
            (
                r["fields"]["jev_status"],
                r["fields"].get("jev_reason"),
                r["fields"].get("jev_fallback"),
            )
            for r in selected
            if r["fields"].get("jev_status")
        )
        by_strategy[producer] = {
            "classification_version": CLASSIFICATION_VERSION,
            "candidate_count": len({r["signal_id"] for r in selected}),
            "candidate_count_basis": "SUPPORTED_DIRECTIONAL_ENTRY_EVENT_SAMPLE_NOT_OPEN_DECISIONS",
            "non_candidate_counts": dict(Counter(_event_class(r) for r in excluded)),
            "non_candidate_reason_counts": [
                {"classification": kind, "reason": reason, "count": count}
                for (kind, reason), count in sorted(
                    Counter(
                        (
                            _event_class(r),
                            r.get("reason") or r.get("receipt_reason") or "UNKNOWN",
                        )
                        for r in excluded
                    ).items()
                )
            ],
            "lifecycle_counts": dict(Counter(_lifecycle(r) for r in selected)),
            "stage_counts": dict(Counter(_stage(r) for r in selected)),
            "reason_counts": [
                {"stage": stage, "reason": reason, "count": count}
                for (stage, reason), count in sorted(selected_reasons.items())
            ],
            "jev_outcomes": [
                {
                    "status": status,
                    "reason": reason,
                    "fallback_cause": cause,
                    "count": count,
                }
                for (status, reason, cause), count in sorted(
                    selected_jev.items(), key=lambda p: str(p[0])
                )
            ],
            "rejection_details": _rejection_details(selected),
        }
    return {
        "signal_count": len(signals),
        "signal_sources": dict(sources),
        "first_signal_ms": min(signals.values()) if signals else None,
        "last_signal_ms": max(signals.values()) if signals else None,
        "stage_counts": dict(stages),
        "reason_counts": [
            {"stage": stage, "reason": reason, "count": count}
            for (stage, reason), count in sorted(reasons.items())
        ],
        "jev_outcomes": [
            {
                "status": status,
                "reason": reason,
                "fallback_cause": cause,
                "count": count,
            }
            for (status, reason, cause), count in sorted(
                jev.items(), key=lambda p: str(p[0])
            )
        ],
        "evidence": samples,
        "by_strategy": by_strategy,
        "coverage": {
            "signals": "TRUNCATED" if truncated else "BOUNDED_QUERY_COMPLETE",
            "absence_meaning": "NO_RETAINED_SHARED_SIGNAL_IN_WINDOW"
            if not signals
            else None,
            "retention_completeness": "NOT_PROVEN",
            "account_route_active_for_full_day": "NOT_PROVEN",
            "signal_inbox_scope": "SHARED_ENVIRONMENT_NOT_ACCOUNT",
            "decision_scope": "EXACT_ACCOUNT_S6_S8_CONSUMERS",
            "examined_consumer_records": len(records),
            "evidence_examples_truncated": len(samples) < len(records),
            "evidence_examples_basis": "SOURCE_WINDOW_CONSUMER_THEN_REASON_COVERAGE",
        },
    }


def _signal_records(rows, consumers, limits):
    ids, records, counts = set(), [], Counter()
    for row in rows:
        (
            identity,
            source,
            event,
            observed,
            received,
            consumer,
            decision,
            action,
            side,
            evidence,
            decided,
            reason,
            digest,
            version,
            fields,
            receipt,
            receipt_reason,
            intent,
            completed,
            expires,
            sampled,
            rejection_detail,
        ) = row
        if not sampled:
            continue
        bucket = ((observed + OFFSET_MS) % DAY_MS) // SIGNAL_WINDOW_MS
        key = (source, bucket)
        cap = _window_cap(limits[source], bucket)
        if identity not in ids:
            if counts[key] >= cap:
                continue
            ids.add(identity)
            counts[key] += 1
        records.append(
            {
                "signal_id": identity,
                "source": source,
                "event": _text(event),
                "observed_ms": observed,
                "expires_ms": expires,
                "received_ms": received,
                "consumer_strategy": consumers[consumer],
                "decision_id": decision,
                "action": action,
                "side": side,
                "evidence_ref": evidence,
                "decided_ms": decided,
                "reason": _text(reason),
                "config_digest": digest,
                "strategy_version": _text(version),
                "fields": {k: _text(v) for k, v in fields.items() if v is not None},
                "receipt_outcome": receipt,
                "receipt_reason": _text(receipt_reason),
                "intent_id": intent,
                "completed_ms": completed,
                "rejection_detail": _frozen_details(rejection_detail),
            }
        )
    return records


def _window_cap(limit, bucket):
    return limit // SIGNAL_WINDOWS + (bucket < limit % SIGNAL_WINDOWS)


def _window_coverage(rows, *, start_ms, limits, error):
    counts = Counter(
        (source, ((observed + OFFSET_MS) % DAY_MS) // SIGNAL_WINDOW_MS)
        for _, source, observed in {(r[0], r[1], r[3]) for r in rows}
    )
    windows = []
    for bucket in range(SIGNAL_WINDOWS):
        sources = {}
        for source, limit in limits.items():
            cap, loaded = _window_cap(limit, bucket), counts[(source, bucket)]
            status = (
                "UNAVAILABLE"
                if error
                else "NOT_SAMPLED"
                if limit == 0
                else "TRUNCATED"
                if loaded > cap
                else "BOUNDED_QUERY_COMPLETE"
            )
            sources[source] = {
                "status": status,
                "sample_limit": cap,
                "examined_unique_signals": None
                if error or limit == 0
                else min(loaded, cap),
                "observed_lower_bound": None if error or limit == 0 else loaded,
                "sentinel_observed": None if error or limit == 0 else loaded > cap,
            }
        windows.append(
            {
                "start_ms": start_ms + bucket * SIGNAL_WINDOW_MS,
                "end_ms": start_ms + (bucket + 1) * SIGNAL_WINDOW_MS,
                "sources": sources,
            }
        )
    return windows


def _intent_records(rows, *, start_ms, end_ms, limit):
    records = []
    with localcontext() as ctx:
        ctx.prec = 80
        for row in rows[:limit]:
            (
                identity,
                signal,
                evidence,
                symbol,
                side,
                producer,
                decided,
                source,
                entry_mode,
                digest,
                version,
                oq,
                cq,
                onot,
                cnot,
                fees,
                opened,
                last_close,
                close_orders,
                fill_count,
                terminal_event,
                terminal_status,
                terminal_reason,
                terminal_ms,
                close_order,
                close_reason,
                close_origin,
                close_kind,
                orders,
                stop_fraction,
                planned_price,
            ) = row
            opening, closing = Decimal(oq), Decimal(cq)
            # Carry-in must overlap the review day. Newly-created unfilled
            # intents belong to the day; older unfilled requests do not.
            if opened is None and decided < start_ms:
                continue
            if opened is not None and (
                opened >= end_ms
                or (
                    opening == closing
                    and last_close is not None
                    and last_close < start_ms
                )
            ):
                continue
            valid = opening >= closing >= 0 and side in {"BUY", "SELL"}
            timing_valid = (
                opening == 0 or (type(opened) is int and decided <= opened)
            ) and (
                closing == 0
                or (
                    type(opened) is int
                    and type(last_close) is int
                    and opened <= last_close
                )
            )
            validation_error = (
                "INCONSISTENT_FILLS"
                if not valid
                else "INVALID_FILL_TIMING"
                if not timing_valid
                else None
            )
            valid = valid and timing_valid
            actual_closed = last_close if opening > 0 and opening == closing else None
            records.append(
                {
                    "id": identity,
                    "signal_id": signal,
                    "evidence_ref": evidence,
                    "symbol": symbol,
                    "side": side,
                    "producer": producer,
                    "decided_ms": decided,
                    "source": source or "UNKNOWN",
                    "entry_mode": _text(entry_mode),
                    "config_digest": digest,
                    "strategy_version": _text(version),
                    "opened_ms": opened,
                    "closed_ms": actual_closed,
                    "last_reduction_ms": last_close,
                    "opening_quantity": oq,
                    "closing_quantity": cq,
                    "remaining_quantity": str(opening - closing) if valid else None,
                    "quantity": oq,
                    "opening_notional": onot,
                    "closing_notional": cnot,
                    "entry_vwap": str(Decimal(onot) / opening)
                    if valid and opening and onot
                    else None,
                    "exit_fill_vwap": str(Decimal(cnot) / closing)
                    if valid and closing and cnot
                    else None,
                    "fees_usdt": fees,
                    "close_order_count": close_orders,
                    "fill_count": fill_count,
                    "partial_exit_observed": 0 < closing < opening,
                    "multiple_reductions_observed": close_orders > 1,
                    "fills_valid": valid,
                    "fill_validation_error": validation_error,
                    "full_fill_path_validated": False,
                    "carry_in": opened is not None and opened < start_ms,
                    "terminal_event_ref": terminal_event,
                    "terminal_status": terminal_status,
                    "terminal_reason": _text(terminal_reason),
                    "terminal_ms": terminal_ms,
                    "last_close_order_ref": close_order,
                    "exit_reason": _text(
                        recorded_exit_reason(close_reason, close_origin, close_kind)
                    ),
                    "exit_origin": _text(close_origin),
                    "orders": [
                        {**o, "reason": _text(o.get("reason"))} for o in orders[:20]
                    ],
                    "orders_truncated": len(orders) > 20,
                    "planned_stop_fraction": stop_fraction,
                    "planned_entry_price": planned_price,
                }
            )
    return records


def _bounded_read(conn, deadline, operation):
    """Savepoint isolation keeps one unavailable symbol from losing the day.

    Error details are never serialized; SQLSTATE yields only a fixed diagnostic
    code. The caller still sees missing coverage rather than fabricated zeros.
    """
    if time.monotonic() >= deadline:
        return None, "READ_BUDGET_EXHAUSTED"
    try:
        with conn.transaction():
            return operation(), None
    except Exception as exc:  # noqa: BLE001 - bounded research cannot block execution
        return None, {
            "57014": "QUERY_TIMEOUT",
            "42501": "READ_PERMISSION_MISSING",
            "42P01": "READ_SCHEMA_UNAVAILABLE",
        }.get(getattr(exc, "sqlstate", None), "READ_FAILED")


def load_daily_mover_evidence(
    conn,
    scope,
    *,
    start_ms,
    end_ms,
    symbols,
    as_of_ms=None,
    max_signals_per_symbol=250,
    max_trades=500,
    max_read_seconds=20,
):
    """Read-only single UTC+8 day, <=20 symbols and <=500 scoped trade labels.

    ``as_of_ms`` is evidence knowledge time, not the day's market endpoint.
    Later settlement is usable only when this explicit cutoff admits it. Current
    accounting-revision validation may invalidate historical labels; no old
    revision is silently reused. This function does not commit or perform I/O.
    """
    if not isinstance(scope, AccountScope) or scope.environment not in {
        "SANDBOX",
        "LIVE",
    }:
        raise ValueError("DAILY_MOVER_ACCOUNT_SCOPE_REQUIRED")
    as_of_ms = end_ms if as_of_ms is None else as_of_ms
    if (
        type(start_ms) is not int
        or type(end_ms) is not int
        or type(as_of_ms) is not int
        or start_ms < 0
        or end_ms - start_ms != DAY_MS
        or (start_ms + OFFSET_MS) % DAY_MS
        or not end_ms <= as_of_ms <= 2**53 - 1
    ):
        raise ValueError("DAILY_MOVER_COMPLETED_LOCAL_DAY_REQUIRED")
    if (
        not isinstance(symbols, (list, tuple))
        or not 1 <= len(symbols) <= 20
        or any(not _public_symbol(s) for s in symbols)
        or len(set(symbols)) != len(symbols)
    ):
        raise ValueError("DAILY_MOVER_SYMBOL_BOUND")
    if (
        type(max_signals_per_symbol) is not int
        or not 1 <= max_signals_per_symbol <= 250
        or type(max_trades) is not int
        or not len(symbols) <= max_trades <= 500
        or type(max_read_seconds) is not int
        or not 1 <= max_read_seconds <= 60
    ):
        raise ValueError("DAILY_MOVER_QUERY_BOUND")
    if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
        raise ValueError("DAILY_MOVER_READ_ONLY_REQUIRED")
    if conn.execute("SHOW transaction_isolation").fetchone()[0] not in {
        "repeatable read",
        "serializable",
    }:
        raise ValueError("DAILY_MOVER_STABLE_SNAPSHOT_REQUIRED")
    conn.execute("SET LOCAL statement_timeout='3s'")
    conn.execute("SET LOCAL lock_timeout='100ms'")
    consumers = {
        StrategyScope(**asdict(scope), producer=p).consumer: p for p in ("s6", "s8")
    }
    reports, labels_needed = {}, []
    deadline = time.monotonic() + max_read_seconds
    # Fair per-symbol limits prevent the busiest contract from hiding every
    # other mover. An old carry-in outside the lookback remains an explicit gap.
    per_symbol = max_trades // len(symbols)
    # A dense internal scanner must not hide the day's rarer TV or quant input.
    source_limits = {
        source: max_signals_per_symbol // len(_SOURCES)
        + (offset < max_signals_per_symbol % len(_SOURCES))
        for offset, source in enumerate(_SOURCES)
    }
    lookback = max(0, start_ms - 90 * DAY_MS)
    for symbol in symbols:
        rows, signal_error = _bounded_read(
            conn,
            deadline,
            lambda symbol=symbol: conn.execute(
                _SIGNALS_SQL,
                (
                    list(_SOURCES),
                    list(source_limits.values()),
                    scope.environment,
                    start_ms,
                    start_ms,
                    end_ms,
                    symbol,
                    as_of_ms,
                    list(consumers),
                    as_of_ms,
                    as_of_ms,
                    as_of_ms,
                ),
            ).fetchall(),
        )
        rows = rows or []
        records = _signal_records(rows, consumers, source_limits)
        time_windows = _window_coverage(
            rows, start_ms=start_ms, limits=source_limits, error=signal_error
        )
        source_coverage = {
            source: "UNAVAILABLE"
            if signal_error
            else "NOT_SAMPLED"
            if cap == 0
            else "TRUNCATED"
            if any(w["sources"][source]["status"] == "TRUNCATED" for w in time_windows)
            else "BOUNDED_QUERY_COMPLETE"
            for source, cap in source_limits.items()
        }
        report = summarize_symbol_evidence(
            records,
            truncated=any(
                status in {"TRUNCATED", "NOT_SAMPLED"}
                for status in source_coverage.values()
            ),
        )
        report["coverage"].update(
            signal_sources=source_coverage,
            signal_limit_by_source=source_limits,
            signal_sample_basis=SIGNAL_SAMPLE_BASIS,
            signal_time_windows=time_windows,
        )
        report["engine_symbol_supported"] = is_supported_symbol(
            symbol, quote_assets=("USDT",)
        )
        if signal_error:
            report["signal_count"] = None
            report["coverage"].update(
                signals="UNAVAILABLE", signal_error=signal_error, absence_meaning=None
            )
        intent_rows, intent_error = _bounded_read(
            conn,
            deadline,
            lambda symbol=symbol: conn.execute(
                _INTENTS_SQL,
                (
                    *asdict(scope).values(),
                    symbol,
                    lookback,
                    end_ms,
                    as_of_ms,
                    per_symbol + 1,
                    as_of_ms,
                    as_of_ms,
                    as_of_ms,
                    as_of_ms,
                    as_of_ms,
                    as_of_ms,
                    as_of_ms,
                ),
            ).fetchall(),
        )
        intent_rows = intent_rows or []
        report["trades"] = _intent_records(
            intent_rows, start_ms=start_ms, end_ms=end_ms, limit=per_symbol
        )
        report["coverage"].update(
            intents="TRUNCATED"
            if len(intent_rows) > per_symbol
            else "BOUNDED_QUERY_COMPLETE",
            intent_candidates_examined=min(len(intent_rows), per_symbol),
            intent_limit=per_symbol,
            intent_lookback_start_ms=lookback,
            older_carry_in="NOT_EXAMINED",
        )
        if intent_error:
            report["coverage"].update(intents="UNAVAILABLE", intent_error=intent_error)
        labels_needed.extend(t["id"] for t in report["trades"])
        reports[symbol] = report
    labels = {}
    for offset in range(0, len(labels_needed), 100):
        selected = labels_needed[offset : offset + 100]
        loaded, error = _bounded_read(
            conn,
            deadline,
            lambda selected=selected: outcomes(conn, scope, selected, now=as_of_ms),
        )
        labels.update(
            loaded
            if error is None
            else {
                identity: {
                    "state": "LABEL_UNAVAILABLE",
                    "net_pnl": None,
                    "read_error": error,
                }
                for identity in selected
            }
        )
    for report in reports.values():
        execution_counts, execution_reasons = Counter(), Counter()
        for trade in report["trades"]:
            label = labels.get(
                trade["id"], {"state": "LABEL_UNAVAILABLE", "net_pnl": None}
            )
            settled = label["state"] == "SETTLED" and trade["fills_valid"]
            trade.update(
                settlement_state=trade["fill_validation_error"] or label["state"],
                settled_ms=label.get("settled_ms"),
                settlement_revision=label.get("settlement_revision"),
                net_pnl=label.get("net_pnl") if settled else None,
                net_pnl_available=settled,
                label_read_error=label.get("read_error"),
                status="FILLED_SETTLED"
                if settled
                else "FILLED_UNSETTLED"
                if Decimal(trade["opening_quantity"]) > 0
                else "NO_EXECUTION_FILL",
            )
            # Never infer funding from gross minus net: this would conflate cash
            # corrections and fees. Complete net already includes settled costs.
            trade["funding_usdt"] = None
            trade["funding_status"] = "NOT_SEPARATELY_LOADED"
            if settled:
                try:
                    with localcontext() as context:
                        context.prec = 80
                        gross = (
                            Decimal(trade["closing_notional"])
                            - Decimal(trade["opening_notional"])
                        ) * (1 if trade["side"] == "BUY" else -1)
                    trade["gross_pnl_usdt"] = str(gross)
                except (InvalidOperation, TypeError):
                    trade["gross_pnl_usdt"] = None
            else:
                trade["gross_pnl_usdt"] = None
            execution_counts[trade["status"]] += 1
            if trade["terminal_event_ref"] and trade["terminal_reason"]:
                stage = (
                    "EXECUTION_EXPIRED_NOT_STRATEGY_REJECTION"
                    if trade["terminal_status"] == "EXPIRED"
                    else "RECORDED_INTENT_TERMINATION"
                )
                execution_reasons[(stage, trade["terminal_reason"])] += 1
            for order in trade["orders"]:
                if (
                    order["leg"] == "OPEN"
                    and order.get("reason")
                    and order.get("status") in {"REJECTED", "CANCELLED"}
                ):
                    execution_reasons[
                        ("RECORDED_OPEN_ORDER_TERMINATION", order["reason"])
                    ] += 1
        report["execution_counts"] = dict(execution_counts)
        report["execution_reason_counts"] = [
            {"stage": stage, "reason": reason, "count": count}
            for (stage, reason), count in sorted(execution_reasons.items())
        ]
        report["settlement_states"] = dict(
            Counter(t["settlement_state"] for t in report["trades"])
        )
    return {
        "schema": SCHEMA,
        "account_scope": asdict(scope),
        "timezone": "Asia/Shanghai",
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "as_of_ms": as_of_ms,
        "symbols": reports,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "limits": {
            "signals_per_symbol": max_signals_per_symbol,
            "trades": max_trades,
            "statement_timeout_ms": 3000,
            "label_batches": (len(labels_needed) + 99) // 100,
            "read_budget_seconds": max_read_seconds,
        },
        "limitations": [
            "Shared signals do not prove an account was active or obliged to consume them; route history and retention completeness are not proven.",
            "Missing records remain unknown; no retained signal is not proof of absent market opportunity. Expiry is not a strategy rejection.",
            "Decision counts are per s6/s8 consumer; signal counts deduplicate signal identity. Truncated queries are lower bounds, not a complete day.",
            "Directional classifications use supported entry events only; opposite-direction, non-entry and unknown events are separate audit counts.",
            "Uniform-rank sampling spans each six-hour source window; representative examples are bounded and do not reconstruct every decision.",
            "Late expiry receipts alone do not establish maintenance origin. Missing frozen operands or thresholds are not filled from current policy.",
            "Only immutable recorded decisions/receipts/events support refusal reasons; transient risk failures without durable evidence remain unknown.",
            "JEV fallback, skipped/unavailable and veto are separate; no model record does not imply model rejection or approval.",
            "Actual account fills are not executable prices on another environment or venue; public LIVE mover paths cannot establish SANDBOX attainable PnL.",
            "Current-revision complete USDT settlement is required for net PnL; costs are not subtracted twice. Funding is not separately reconstructed.",
            "Daily mover selection is hindsight research, not proof of an ex-ante signal or a requirement to buy the low and sell the high.",
        ],
    }
