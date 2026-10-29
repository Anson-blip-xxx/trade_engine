"""Offline, read-only comparison of actual trades; never an execution service.

Output files are disposable analysis exports, not runtime business state.
Historical cohorts are descriptive; no alternate fills or saved-loss claims.
"""

import argparse
import csv
import hashlib
import json
import os
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from services.v2_review_risk import risk_statistics
from services.v2_strategy_analytics import statistics
from services.v2_trade_review import trade_review
from v2_core.account_risk import AccountScope
from v2_core.directional import supports_event

DAY = 86_400_000


def band(value, thresholds):
    if value is None:
        return "UNKNOWN"
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("NONFINITE_FEATURE")
    for upper, label in thresholds:
        if value < Decimal(str(upper)):
            return label
    return "HIGH"


def summarize(rows):
    result = statistics(rows)
    result["initial_risk_units"] = risk_statistics(rows)
    settled = [r for r in rows if r["net_pnl"] is not None and not r["maintenance"]]
    result["actual_opened"] = len(rows)
    for name in ("fees_usdt", "funding_usdt", "other_cash_usdt", "gross_usdt"):
        values = [Decimal(r[name]) for r in settled if r.get(name) is not None]
        result[name] = (
            str(sum(values, Decimal(0))) if len(values) == len(settled) else None
        )
        result[name + "_missing"] = len(settled) - len(values)
    pnl = [Decimal(r["net_pnl"]) for r in settled]
    wins = [v for v in pnl if v > 0]
    losses = [v for v in pnl if v < 0]
    result["average_win_usdt"] = str(sum(wins) / len(wins)) if wins else None
    result["average_loss_usdt"] = str(sum(losses) / len(losses)) if losses else None
    result["cost_flipped_winners"] = sum(
        Decimal(r["gross_usdt"]) > 0 and Decimal(r["net_pnl"]) < 0
        for r in settled
        if r.get("gross_usdt") is not None
    )
    peak = cumulative = drawdown = Decimal(0)
    for r in sorted(settled, key=lambda x: (x["closed_ms"], x["id"])):
        cumulative += Decimal(r["net_pnl"])
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    result["closed_trade_cumulative_drawdown_usdt"] = str(drawdown)
    result["drawdown_is_account_equity_drawdown"] = False
    result["cash_reconciliation_mismatches"] = sum(
        r.get("cash_residual_usdt") is not None
        and Decimal(r["cash_residual_usdt"]) != 0
        for r in settled
    )
    for positive, label in ((True, "winners"), (False, "losers")):
        times = [
            Decimal(r["held_minutes"])
            for r in settled
            if (Decimal(r["net_pnl"]) > 0 if positive else Decimal(r["net_pnl"]) < 0)
        ]
        result[label + "_median_held_minutes"] = str(median(times)) if times else None
    return result


def group(rows, fields):
    groups = defaultdict(list)
    for row in rows:
        groups[
            tuple(row.get(f) if row.get(f) is not None else "UNKNOWN" for f in fields)
        ].append(row)
    return [
        {**dict(zip(fields, key, strict=True)), **summarize(values)}
        for key, values in sorted(groups.items(), key=lambda item: str(item[0]))
    ]


def build_report(rows, *, as_of_ms):
    with localcontext() as ctx:
        ctx.prec = 80
        return _build_report(rows, as_of_ms=as_of_ms)


def _build_report(rows, *, as_of_ms):
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("DUPLICATE_EPISODE")
    if any(r["opened_ms"] > as_of_ms for r in rows):
        raise ValueError("FUTURE_OPEN")
    chosen = [r for r in rows if as_of_ms - 14 * DAY <= r["opened_ms"] <= as_of_ms]
    dimensions = (
        "source",
        "side",
        "entry_mode",
        "regime",
        "fusion",
        "score_band",
        "raw_strength_band",
        "spread_band",
        "holding_band",
        "exit_reason",
        "config_digest",
        "entry_controls_version",
    )
    diagnostics = [
        {"label": "ALL_ACTUAL", **summarize(chosen)},
        {
            "label": "EXCHANGE_LIQUIDATION",
            **summarize([r for r in chosen if r["liquidation"]]),
        },
        {
            "label": "OTHER_ACTUAL_EXITS_NOT_A_COUNTERFACTUAL",
            **summarize([r for r in chosen if not r["liquidation"]]),
        },
    ]
    return {
        "kind": "ACTUAL_SETTLED_COHORT_COMPARISON_NOT_BACKTEST",
        "as_of": datetime.fromtimestamp(
            as_of_ms / 1000, ZoneInfo("Asia/Shanghai")
        ).isoformat(),
        "as_of_ms": as_of_ms,
        "timezone": "Asia/Shanghai",
        "windows_by_actual_open_time": [
            {
                "days": days,
                **summarize(
                    [
                        r
                        for r in rows
                        if as_of_ms - days * DAY <= r["opened_ms"] <= as_of_ms
                    ]
                ),
            }
            for days in (1, 7, 14, 30)
        ],
        "groups_14d": {field: group(chosen, (field,)) for field in dimensions},
        "source_side_14d": group(chosen, ("source", "side")),
        "entry_mode_side_14d": group(chosen, ("entry_mode", "side")),
        "entry_mode_side_score_14d": group(
            chosen, ("entry_mode", "side", "score_band")
        ),
        "side_regime_14d": group(chosen, ("side", "regime")),
        "weekly_stability": [
            {
                "period": label,
                "start_ms": start,
                "end_ms_exclusive": end,
                "summary": summarize(
                    [r for r in rows if start <= r["opened_ms"] < end]
                ),
                "entry_modes": group(
                    [r for r in rows if start <= r["opened_ms"] < end], ("entry_mode",)
                ),
                "sides": group(
                    [r for r in rows if start <= r["opened_ms"] < end], ("side",)
                ),
            }
            for label, start, end in (
                ("EARLIER_7D", as_of_ms - 14 * DAY, as_of_ms - 7 * DAY),
                ("LATER_7D", as_of_ms - 7 * DAY, as_of_ms + 1),
            )
        ],
        "execution_attribution_14d": diagnostics,
        "worst_10_actual_14d": sorted(
            [r for r in chosen if r["net_pnl"] is not None and not r["maintenance"]],
            key=lambda r: Decimal(r["net_pnl"]),
        )[:10],
        "trades": rows,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "external_data_comparison_status": "NOT_AVAILABLE_NO_POINT_IN_TIME_DEX_OR_LLM_PREDICTIONS",
        "limitations": [
            "Group PnL is actual historical cohort PnL, not a simulated alternative strategy or avoided loss.",
            "The last 14 days and weekly slices mix existing configurations and market regimes; no causal claim from release date.",
            "Only genuinely filled trades appear. Rejected/missed opportunities require a separate signal-level forward comparison.",
            "Win rate and signal strength are not calibrated probabilities. Small groups do not authorize sizing or policy changes.",
            "Holding time and exit reason are outcome diagnostics, never allowed as entry-time filter features.",
            "Net PnL already includes fees, funding and cash corrections; cost columns must not be subtracted again.",
            "Liquidation losses remain in the baseline; showing other exits separately is diagnostic, not an improved strategy return.",
            "Closed-trade cumulative drawdown excludes intratrade unrealized PnL and is not account equity drawdown.",
            "R uses actual filled exposure and the original planned stop; it does not prove the venue accepted that protection order.",
            "No historical DEX state or LLM judgement is reconstructed from today's data; no external strategy profitability claim.",
            "Cash adjustments have event timestamps but no ingestion timestamps; retrospective cash decomposition is not a point-in-time backtest input.",
            "Trade coverage is limited to the active account's s6/s8 actual fills; a decision/open-time boundary gap causes export refusal.",
            "Open/unsettled trades and missing evidence remain visible; full intratrade profit giveback requires complete archived price paths.",
        ],
    }


def load(conn, *, as_of_ms):
    if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
        raise ValueError("READ_ONLY_REQUIRED")
    scopes = conn.execute(
        "SELECT a.exchange,a.account_id,a.environment,a.product,a.display_name "
        "FROM v2_execution_routes r JOIN v2_tenant_accounts a ON a.tenant_id=r.tenant_id "
        "AND a.registry_id=r.target_registry WHERE r.environment='SANDBOX'"
    ).fetchall()
    if len(scopes) != 1:
        raise ValueError("UNIQUE_SANDBOX_ROUTE_REQUIRED")
    scope = AccountScope(*scopes[0][:4])
    # trade_review selects by intent creation. Refuse rather than silently lose
    # a pre-window decision whose first actual fill belongs inside our window.
    boundary_gap = conn.execute(
        """SELECT EXISTS(SELECT 1 FROM v2_trade_intents i
        WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
          AND i.producer IN ('s6','s8') AND i.created_at<to_timestamp(%s/1000.0)
          AND EXISTS(SELECT 1 FROM v2_orders o JOIN v2_fills f USING(order_id)
            WHERE o.episode_id=i.intent_id AND o.leg='OPEN'
              AND f.occurred_at_ms BETWEEN %s AND %s
              AND f.created_at<=to_timestamp(%s/1000.0)))""",
        (*scopes[0][:4], as_of_ms - 30 * DAY, as_of_ms - 30 * DAY, as_of_ms, as_of_ms),
    ).fetchone()[0]
    if boundary_gap:
        raise ValueError("REFUSE_INTENT_OPEN_TIME_BOUNDARY_GAP")
    review = trade_review(conn, scope, days=30, now_ms=as_of_ms, limit=500)
    if review["truncated"]:
        raise ValueError("REFUSE_TRUNCATED_BASELINE")
    rows = review["trades"]
    ids = [r["id"] for r in rows]
    extra = conn.execute(
        """SELECT i.intent_id::text,
        e.snapshot#>'{features,context,signal_fusion}',
        e.snapshot#>>'{features,context,directional,regime}',
        e.snapshot#>>'{features,evaluation,leverage_decision,venue,spread_fraction}',
        e.snapshot#>>'{features,evaluation,entry_controls,version}',
        EXISTS(SELECT 1 FROM v2_orders o JOIN v2_fills f USING(order_id)
          WHERE o.episode_id=i.intent_id AND o.leg='CLOSE'
          AND o.request_evidence->>'origin'='BINANCE_EXTERNAL_CLOSE'
          AND o.request_evidence->>'kind'='LIQUIDATION'
          AND f.occurred_at_ms<=%s AND f.created_at<=to_timestamp(%s/1000.0)),
        e.evidence_ref
        FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref)
        WHERE i.intent_id=ANY(%s::uuid[])""",
        (as_of_ms, as_of_ms, ids),
    ).fetchall()
    extras = {r[0]: r[1:] for r in extra}
    cash = conn.execute(
        "SELECT episode_id::text,kind,sum(amount)::text FROM v2_cash_adjustments "
        "WHERE episode_id=ANY(%s::uuid[]) AND currency='USDT' AND occurred_at_ms<=%s "
        "GROUP BY episode_id,kind",
        (ids, as_of_ms),
    ).fetchall()
    totals = defaultdict(dict)
    for episode, kind, value in cash:
        totals[episode][kind] = value
    for row in rows:
        fusion, regime, spread, controls, liquidation, evidence_ref = extras[row["id"]]
        if fusion is None:
            relationship = "UNKNOWN"
        else:
            tags = {p.get("relationship") for p in fusion.get("peers", [])}
            relationship = (
                "MIXED"
                if {"CONFIRMS", "CONFLICTS"} <= tags
                else (
                    "CONFIRMS"
                    if "CONFIRMS" in tags
                    else "CONFLICTS"
                    if "CONFLICTS" in tags
                    else "NO_PEER"
                )
            )
        values = totals[row["id"]]
        funding = Decimal(values.get("FUNDING", "0"))
        other = sum(
            (Decimal(v) for k, v in values.items() if k != "FUNDING"), Decimal(0)
        )
        gross = None
        if row["closed_ms"] is not None:
            gross = (
                Decimal(row["closing_notional"]) - Decimal(row["opening_notional"])
            ) * (1 if row["side"] == "BUY" else -1)
        residual = None
        if (
            gross is not None
            and row["net_pnl"] is not None
            and row["fees_usdt"] is not None
        ):
            residual = Decimal(row["net_pnl"]) - (
                gross - Decimal(row["fees_usdt"]) + funding + other
            )
        row.update(
            regime=regime,
            fusion=relationship,
            spread_fraction=spread,
            entry_controls_version=controls,
            liquidation=liquidation,
            evidence_ref=evidence_ref,
            gross_usdt=str(gross) if gross is not None else None,
            funding_usdt=str(funding),
            other_cash_usdt=str(other),
            cash_residual_usdt=str(residual) if residual is not None else None,
            score_band=band(row["score"], ((60, "<60"), (80, "60-79"), (100, "80-99"))),
            raw_strength_band=band(row["raw_strength"], ((80, "<80"), (99, "80-98"))),
            spread_band=band(spread, (("0.001", "<10bps"), ("0.005", "10-49bps"))),
            holding_band=band(
                row["held_minutes"],
                ((30, "<30m"), (120, "30-119m"), (360, "2-6h"), (1440, "6-24h")),
            ),
        )
        if liquidation:
            row["exit_reason"] = "EXCHANGE_LIQUIDATION"
        row["opened_at"] = datetime.fromtimestamp(
            row["opened_ms"] / 1000, ZoneInfo("Asia/Shanghai")
        ).isoformat()
        row["closed_at"] = (
            datetime.fromtimestamp(
                row["closed_ms"] / 1000, ZoneInfo("Asia/Shanghai")
            ).isoformat()
            if row["closed_ms"]
            else None
        )
    return rows, {"alias": scopes[0][4], "scope": asdict(scope), "truncated": False}


def load_tv_funnel(conn, scope, *, as_of_ms, max_signals=10000):
    """Bounded inbound cohort; two consumer attempts are not two TV signals."""
    if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
        raise ValueError("READ_ONLY_REQUIRED")
    if type(max_signals) is not int or not 1 <= max_signals <= 10000:
        raise ValueError("TV_COHORT_LIMIT_INVALID")
    start = as_of_ms - 14 * DAY
    signals = conn.execute(
        "SELECT signal_id,received_at,snapshot->>'signal' FROM v2_inbound_signals "
        "WHERE environment=%s AND source='tv_bridge' "
        "AND received_at BETWEEN to_timestamp(%s/1000.0) AND to_timestamp(%s/1000.0) "
        "ORDER BY received_at DESC,signal_id LIMIT %s",
        (scope["environment"], start, as_of_ms, max_signals + 1),
    ).fetchall()
    if len(signals) > max_signals:
        raise ValueError("REFUSE_TRUNCATED_TV_COHORT")

    def unique_rows(rows, kind):
        unique = {}
        for row in rows:
            if row[0] in unique and unique[row[0]] != row:
                raise ValueError("CONFLICTING_TV_" + kind)
            unique[row[0]] = row
        return list(unique.values())

    signals = unique_rows(signals, "SIGNAL")
    ids = [r[0] for r in signals]
    id_set = set(ids)
    consumers = []
    all_receipts, all_decisions = set(), set()
    classifications = (
        "direction_applicable",
        "opposite_direction",
        "unsupported_direction",
    )
    classified = {
        name: {"inbound": set(), "receipts": [], "decisions": []}
        for name in classifications
    }

    def counts(rows, label):
        return [
            {label: a, "reason": b, "consumer_signal_count": n}
            for (a, b), n in sorted(
                Counter((r[1], r[2]) for r in rows).items(),
                key=lambda item: (-item[1], str(item[0])),
            )
        ]

    def local_time(value):
        return value.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()

    def summary(inbound, receipts, decisions):
        # Rows are unique per consumer/signal; a union across consumers must
        # still count signal identities only once in every distinct metric.
        receipt_ids = {r[0] for r in receipts}
        decision_ids = {r[0] for r in decisions}
        expired_receipts = {r[0] for r in receipts if r[1] == "EXPIRED"}
        expired_decisions = {r[0] for r in decisions if r[1] == "EXPIRED"}
        return {
            "inbound_distinct_signals": len(inbound),
            "receipt_distinct_signals": len(receipt_ids),
            "decision_distinct_signals": len(decision_ids),
            "unreceipted_distinct_signals": len(inbound - receipt_ids),
            "undecided_distinct_signals": len(inbound - decision_ids),
            "expired_receipt_distinct_signals": len(expired_receipts),
            "expired_decision_distinct_signals": len(expired_decisions),
            "expired_distinct_signals": len(expired_receipts | expired_decisions),
            "receipt_consumer_signal_count": len(receipts),
            "decision_consumer_signal_count": len(decisions),
            "expired_receipt_consumer_signal_count": sum(
                r[1] == "EXPIRED" for r in receipts
            ),
            "expired_decision_consumer_signal_count": sum(
                r[1] == "EXPIRED" for r in decisions
            ),
            "receipt_outcome_reasons": counts(receipts, "outcome"),
            "decision_action_reasons": counts(decisions, "action"),
            "latest_decision_at": local_time(max(r[3] for r in decisions))
            if decisions
            else None,
        }

    for producer in ("s6", "s8"):
        consumer = json.dumps(
            [scope[k] for k in ("exchange", "account_id", "environment", "product")]
            + [producer],
            separators=(",", ":"),
        )
        receipts = conn.execute(
            "SELECT signal_id,outcome,reason,completed_at FROM v2_signal_receipts "
            "WHERE consumer=%s AND signal_id=ANY(%s::uuid[]) "
            "AND completed_at<=to_timestamp(%s/1000.0)",
            (consumer, ids, as_of_ms),
        ).fetchall()
        decisions = conn.execute(
            "SELECT d.signal_id,d.action,e.snapshot->>'rationale',d.created_at "
            "FROM v2_strategy_decisions d JOIN v2_decision_evidence e USING(evidence_ref) "
            "WHERE d.consumer=%s AND d.signal_id=ANY(%s::uuid[]) "
            "AND d.created_at BETWEEN to_timestamp(%s/1000.0) AND to_timestamp(%s/1000.0)",
            (consumer, ids, start, as_of_ms),
        ).fetchall()
        receipts = unique_rows(receipts, "RECEIPT")
        decisions = unique_rows(decisions, "DECISION")
        if any(r[0] not in id_set for r in receipts + decisions):
            raise ValueError("TV_RECORD_OUTSIDE_INBOUND_COHORT")
        all_receipts.update(r[0] for r in receipts)
        all_decisions.update(r[0] for r in decisions)
        consumer_report = {"producer": producer, **summary(id_set, receipts, decisions)}
        buckets = {name: set() for name in classifications}
        opposite = "S8" if producer == "s6" else "S6"
        for identity, _, event in signals:
            category = (
                "direction_applicable"
                if supports_event(producer.upper(), event)
                else "opposite_direction"
                if supports_event(opposite, event)
                else "unsupported_direction"
            )
            buckets[category].add(identity)
        for name, inbound in buckets.items():
            matching_receipts = [r for r in receipts if r[0] in inbound]
            matching_decisions = [r for r in decisions if r[0] in inbound]
            consumer_report[name] = summary(
                inbound, matching_receipts, matching_decisions
            )
            classified[name]["inbound"].update(inbound)
            classified[name]["receipts"].extend(matching_receipts)
            classified[name]["decisions"].extend(matching_decisions)
        consumers.append(consumer_report)
    return {
        "as_of_ms": as_of_ms,
        "window_start_ms": start,
        "cohort_basis": "ENVIRONMENT_INBOUND_RECEIVED_AT_CURRENT_ACCOUNT_CONSUMERS",
        "inbound_distinct_signals": len(ids),
        "latest_inbound_at": local_time(max(r[1] for r in signals))
        if signals
        else None,
        "receipt_distinct_signals_any_consumer": len(all_receipts),
        "decision_distinct_signals_any_consumer": len(all_decisions),
        "consumers": consumers,
        **{
            name: summary(values["inbound"], values["receipts"], values["decisions"])
            for name, values in classified.items()
        },
        "truncated": False,
        "limitations": [
            "Environment inbound signals need not have targeted this account at their arrival time; account route history is not reconstructed.",
            "Consumer counts overlap: do not sum them as distinct signals.",
            "Direction applicability uses the current shared S6/S8 supported-event registry, not a reconstructed historical registry.",
            "Opposite-direction expiries are not missed eligible decisions; unsupported events are a separate cohort, not opposite-direction signals.",
            "Applicable expiries alone do not prove consumer throughput failure: historical safety blocks and account availability require separate attribution.",
            "Classification cohorts overlap across consumers; an eligible signal may also appear in the other consumer's opposite-direction cohort.",
            "No price-path outcome for rejected signals is inferred from this funnel.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of", required=True, help="ISO timestamp with explicit timezone"
    )
    parser.add_argument(
        "--output", required=True, help="New analysis directory; must not exist"
    )
    args = parser.parse_args()
    at = datetime.fromisoformat(args.as_of)
    if at.tzinfo is None:
        raise ValueError("EXPLICIT_TIMEZONE_REQUIRED")
    dsn = os.environ["V2_COMPARISON_READ_DSN"]
    import psycopg

    with localcontext() as ctx:
        ctx.prec = 80
        with psycopg.connect(
            dsn, connect_timeout=5, application_name="v2-offline-trade-comparison"
        ) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute("SET LOCAL search_path TO trade_v2,pg_catalog")
            conn.execute("SET LOCAL statement_timeout='5s'")
            conn.execute("SET LOCAL lock_timeout='250ms'")
            conn.execute("SET LOCAL jit=off")
            rows, account = load(conn, as_of_ms=int(at.timestamp() * 1000))
            funnel = load_tv_funnel(
                conn, account["scope"], as_of_ms=int(at.timestamp() * 1000)
            )
        report = {
            "account": account,
            "tv_funnel_14d": funnel,
            **build_report(rows, as_of_ms=int(at.timestamp() * 1000)),
        }
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    output = Path(args.output)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    (output / "comparison.json").write_text(encoded + "\n", encoding="utf-8")
    columns = (
        "id",
        "symbol",
        "side",
        "source",
        "entry_mode",
        "regime",
        "fusion",
        "score",
        "raw_strength",
        "opened_at",
        "closed_at",
        "held_minutes",
        "gross_usdt",
        "fees_usdt",
        "funding_usdt",
        "other_cash_usdt",
        "net_pnl",
        "cash_residual_usdt",
        "liquidation",
        "exit_reason",
        "config_digest",
        "entry_controls_version",
        "evidence_ref",
    )
    with (output / "trades.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(
        json.dumps(
            {
                "directory": str(output),
                "comparison_file_sha256": hashlib.sha256(
                    (encoded + "\n").encode()
                ).hexdigest(),
                "windows": report["windows_by_actual_open_time"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
