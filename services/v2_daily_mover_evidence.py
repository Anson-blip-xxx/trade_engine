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
from v2_core.account_risk import AccountScope
from v2_core.strategy import StrategyScope

DAY_MS = 86400000
OFFSET_MS = 28800000
SCHEMA = "daily-mover-account-evidence-v1"
SIGNAL_WINDOWS = 4
SIGNAL_WINDOW_MS = DAY_MS // SIGNAL_WINDOWS
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
    (((s.snapshot->>'observed_at')::bigint+28800000) %% 86400000)/21600000 bucket
  FROM v2_inbound_signals s JOIN source_windows w ON s.source=w.source
  WHERE environment=%s
    AND ((snapshot->>'expires_at_ms')::bigint)>%s
    AND (snapshot->>'observed_at')::bigint >= %s
    AND (snapshot->>'observed_at')::bigint < %s
    AND snapshot->>'symbol'=%s
    AND received_at<=to_timestamp(%s/1000.0)
),ranked AS (
  SELECT *,row_number() OVER(PARTITION BY source,bucket ORDER BY observed_ms,signal_id) rn
  FROM base),
chosen AS MATERIALIZED (
  SELECT * FROM ranked WHERE rn<=cap/4+(bucket<cap%%4)::integer+1)
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
    'context_cause',e.snapshot->'features'->'evaluation'->'candidate_context_resolution'->>'cause'),
  r.outcome,r.reason,r.intent_id::text,
  (extract(epoch FROM r.completed_at)*1000)::bigint
FROM chosen s CROSS JOIN unnest(%s::text[]) c(consumer)
LEFT JOIN v2_strategy_decisions d ON d.signal_id=s.signal_id AND d.consumer=c.consumer
  AND d.created_at<=to_timestamp(%s/1000.0)
LEFT JOIN v2_decision_evidence e ON e.evidence_ref=d.evidence_ref
  AND e.created_at<=to_timestamp(%s/1000.0)
LEFT JOIN v2_signal_receipts r ON r.signal_id=s.signal_id AND r.consumer=c.consumer
  AND r.completed_at<=to_timestamp(%s/1000.0)
ORDER BY s.observed_ms,s.signal_id,c.consumer"""

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
  cl.request_evidence->>'origin',
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
        if len(samples) < 50:
            samples.append({**record, "stage": stage})
    by_strategy = {}
    for producer in ("s6", "s8"):
        selected = [r for r in records if r.get("consumer_strategy") == producer]
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
        ) = row
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
                    "exit_reason": _text(close_reason),
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
            signal_sample_basis="SOURCE_FAIR_FOUR_UTC8_WINDOWS_EARLIEST_WITHIN_WINDOW",
            signal_time_windows=time_windows,
        )
        report["engine_symbol_supported"] = bool(
            re.fullmatch(r"[A-Z0-9]{1,32}(USDT|USDC)", symbol)
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
            "Only immutable recorded decisions/receipts/events support refusal reasons; transient risk failures without durable evidence remain unknown.",
            "JEV fallback, skipped/unavailable and veto are separate; no model record does not imply model rejection or approval.",
            "Actual account fills are not executable prices on another environment or venue; public LIVE mover paths cannot establish SANDBOX attainable PnL.",
            "Current-revision complete USDT settlement is required for net PnL; costs are not subtracted twice. Funding is not separately reconstructed.",
            "Daily mover selection is hindsight research, not proof of an ex-ante signal or a requirement to buy the low and sell the high.",
        ],
    }
