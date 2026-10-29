"""Read-only TV/JEV attribution and fixed-horizon public-price markouts.

This is deliberately NOT a portfolio backtest or a reconstructed strategy PnL.
Rejected candidates never acquire an intent, fill, position or capital. The
same delayed-entry convention and costs apply to every cohort. Outcome bars
are read only after the immutable decision has been classified.
"""

import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical
from v2_core.strategy import StrategyScope

MINUTE = 60_000
DAY = 86_400_000
MODEL_VETO_REASONS = {"JEV_TREND_UNSUPPORTED", "JEV_CHASE_RISK_HIGH"}


def _number(value, *, positive=False):
    if type(value) not in (str, int, Decimal) or len(str(value)) > 80:
        raise ValueError("EFFECTIVENESS_NUMBER_INVALID")
    parsed = Decimal(value)
    if not parsed.is_finite() or abs(parsed) >= Decimal("1e20"):
        raise ValueError("EFFECTIVENESS_NUMBER_INVALID")
    if positive and parsed <= 0:
        raise ValueError("EFFECTIVENESS_PRICE_INVALID")
    return parsed


def _text(value):
    return format(value.quantize(Decimal("0.00000001")), "f")


@dataclass(frozen=True)
class EffectivenessPolicy:
    horizons_minutes: tuple = (15, 60, 240)
    fee_per_side_bps: str = "5"
    slippage_per_side_bps: str = "2"
    minimum_matched_pairs: int = 30
    match_window_minutes: int = 360

    def __post_init__(self):
        if (
            not isinstance(self.horizons_minutes, tuple)
            or not 1 <= len(self.horizons_minutes) <= 4
            or any(
                type(n) is not int or not 1 <= n <= 1440 for n in self.horizons_minutes
            )
            or tuple(sorted(set(self.horizons_minutes))) != self.horizons_minutes
        ):
            raise ValueError("EFFECTIVENESS_HORIZONS_INVALID")
        for field in ("fee_per_side_bps", "slippage_per_side_bps"):
            if not 0 <= _number(getattr(self, field)) <= 100:
                raise ValueError("EFFECTIVENESS_COST_INVALID")
        for field, low, high in (
            ("minimum_matched_pairs", 1, 1000),
            ("match_window_minutes", 1, 1440),
        ):
            value = getattr(self, field)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("EFFECTIVENESS_MATCH_BOUND")


def jev_classification(gate):
    """A service failure/disabled gate is never counted as a model opinion."""
    if not isinstance(gate, dict) or not gate:
        return "NOT_REVIEWED"
    status, reason = gate.get("status"), gate.get("reason")
    if status == "PASS" and reason == "JEV_SECOND_FILTER_PASSED":
        return "PASS"
    if status == "VETO" and reason in MODEL_VETO_REASONS:
        return "VETO"
    if status == "FALLBACK" and reason == "JEV_BASELINE_FALLBACK":
        return "FALLBACK"
    if status == "PASS" and reason == "JEV_GATE_DISABLED":
        return "DISABLED"
    if status == "SKIP":
        return "UNAVAILABLE_OR_SKIPPED"
    return "UNCLASSIFIED"


def tv_classification(source, proof):
    if source == "tv_bridge":
        return "TV_DIRECT"
    if not isinstance(proof, dict) or not proof:
        return "NO_FUSION_EVIDENCE"
    # An old cross-source lookup is not proof that it affected admission.
    if not isinstance(proof.get("baseline_plan"), dict) or not isinstance(
        proof.get("fused_plan"), dict
    ):
        return "LEGACY_ASSOCIATION_ONLY"
    if proof.get("version") != "directional-fusion-v1":
        return "UNRECOGNIZED_FUSION_EVIDENCE"
    if proof.get("status") == "OFF":
        return "FUSION_DISABLED"
    if proof.get("status") == "ABSENT":
        return "S3_WITHOUT_TV_PEER"
    if proof.get("status") in {"CONFIRMS", "CONFLICTS"} and any(
        isinstance(peer, dict)
        and peer.get("source") == "tv_bridge"
        and peer.get("relationship") == proof["status"]
        for peer in proof.get("peers", [])
    ):
        return "TV_PEER_" + proof["status"]
    return "FUSION_EVALUATED_WITHOUT_TV_PEER"


def tv_decision_effect(proof):
    baseline, fused = proof.get("baseline_plan"), proof.get("fused_plan")
    if not isinstance(baseline, dict) or not isinstance(fused, dict):
        return "NOT_RECORDED"
    if (
        baseline.get("reason") == "score_below_minimum"
        and fused.get("reason") == "CANDIDATE"
    ):
        return "SOFT_SCORE_ADMISSION_FLIP"
    if proof.get("veto") is True and baseline.get("reason") == "CANDIDATE":
        return "CONFLICT_REJECTED_BASELINE_CANDIDATE"
    return "NO_ADMISSION_FLIP"


def candidate_from_record(record):
    evaluation = record.get("evaluation") or {}
    original = evaluation.get("original_admission", evaluation)
    if not isinstance(original, dict):
        original = {}
    plan = original.get("market_plan") or evaluation.get("market_plan") or {}
    gate = evaluation.get("jev_entry_gate") or {}
    fusion = evaluation.get("signal_fusion") or original.get("signal_fusion") or {}
    side = record.get("side") or {"LONG": "BUY", "SHORT": "SELL"}.get(plan.get("side"))
    # Rejected decisions legitimately have no side column; direction must still
    # come from the frozen plan/config, never from subsequent price movement.
    side = side or {"S6": "BUY", "S8": "SELL"}.get(record.get("strategy"))
    jev = jev_classification(gate)
    return {
        "decision_id": record["decision_id"],
        "evidence_ref": record["evidence_ref"],
        "signal_id": record["signal_id"],
        "symbol": record["symbol"],
        "decided_at_ms": record["decided_at_ms"],
        "side": side,
        "source": record.get("source", "UNKNOWN"),
        "action": record["action"],
        "rationale": record.get("rationale"),
        "regime": record.get("regime") or "UNKNOWN",
        "config_digest": record["config_digest"],
        "strategy_version": record["strategy_version"],
        "jev": jev,
        "jev_reason": gate.get("reason"),
        "fallback_cause": gate.get("fallback_cause"),
        "post_risk_candidate": original.get("admission_authorized") is True,
        "tv": tv_classification(record.get("source"), fusion),
        "tv_status": fusion.get("status"),
        "tv_decision_effect": tv_decision_effect(fusion),
        "tv_baseline_plan": fusion.get("baseline_plan"),
        "tv_fused_plan": fusion.get("fused_plan"),
        "actual_filled": record.get("actual_filled") is True,
        "actual_settled_net_usdt": record.get("actual_settled_net_usdt"),
    }


def fixed_horizon_markout(candidate, bars, *, horizon_minutes, as_of_ms, policy=None):
    """Use the NEXT minute open, never an already observed minute's open.

    Every minute of the horizon must be present and completely closed by as-of.
    Current archives do not prove historical ingestion times; retrospective
    prices are outcomes, never information fed back into the decision.
    """
    policy = policy or EffectivenessPolicy()
    if horizon_minutes not in policy.horizons_minutes:
        raise ValueError("EFFECTIVENESS_HORIZON_NOT_CONFIGURED")
    decided = candidate["decided_at_ms"]
    if type(decided) is not int or type(as_of_ms) is not int or decided < 0:
        raise ValueError("EFFECTIVENESS_CLOCK_INVALID")
    entry = (decided // MINUTE + 1) * MINUTE
    end = entry + horizon_minutes * MINUTE
    result = {
        "horizon_minutes": horizon_minutes,
        "entry_at_ms": entry,
        "exit_at_ms": end,
        "status": "UNKNOWN",
        "net_return_bps": None,
        "gross_return_bps": None,
        "gross_mfe_bps": None,
        "gross_mae_bps": None,
    }
    if decided > as_of_ms or end > as_of_ms:
        return {**result, "status": "CENSORED", "reason": "HORIZON_NOT_YET_CLOSED"}
    if candidate.get("side") not in {"BUY", "SELL"}:
        return {**result, "reason": "DIRECTION_UNKNOWN"}
    found = {}
    try:
        for bar in bars:
            opened = bar.get("t")
            if type(opened) is not int:
                raise ValueError("BAR_TIMESTAMP_INVALID")
            # Ignore all out-of-window bars, including future modifications.
            if not entry <= opened < end:
                continue
            if opened % MINUTE:
                raise ValueError("BAR_ALIGNMENT_INVALID")
            selected = {k: _number(bar[k], positive=True) for k in ("o", "h", "l", "c")}
            if not (
                selected["l"]
                <= min(selected["o"], selected["c"])
                <= max(selected["o"], selected["c"])
                <= selected["h"]
            ):
                raise ValueError("BAR_OHLC_INVALID")
            if opened in found and found[opened] != selected:
                raise ValueError("BAR_DUPLICATE_CONFLICT")
            found[opened] = selected
        if set(found) != set(range(entry, end, MINUTE)):
            return {
                **result,
                "reason": "BAR_HISTORY_INCOMPLETE",
                "observed_bars": len(found),
                "required_bars": horizon_minutes,
            }
        with localcontext() as ctx:
            ctx.prec = 50
            direction = Decimal(1 if candidate["side"] == "BUY" else -1)
            opened, closed = found[entry]["o"], found[end - MINUTE]["c"]
            slip = _number(policy.slippage_per_side_bps) / 10000
            execution_entry = opened * (1 + direction * slip)
            execution_exit = closed * (1 - direction * slip)
            gross = direction * (closed / opened - 1) * 10000
            fee = _number(policy.fee_per_side_bps) * (
                1 + execution_exit / execution_entry
            )
            net = direction * (execution_exit / execution_entry - 1) * 10000 - fee
            excursions = [
                direction * (price / opened - 1) * 10000
                for b in found.values()
                for price in (b["h"], b["l"])
            ]
            return {
                **result,
                "status": "COMPLETE",
                "reason": "MARKOUT_NOT_STRATEGY_PNL",
                "gross_return_bps": _text(gross),
                "net_return_bps": _text(net),
                "gross_mfe_bps": _text(max(Decimal(0), *excursions)),
                "gross_mae_bps": _text(min(Decimal(0), *excursions)),
                "modeled_fee_bps": _text(fee),
                "observed_bars": len(found),
                "required_bars": horizon_minutes,
            }
    except (ValueError, KeyError, ArithmeticError, TypeError):
        return {**result, "reason": "BAR_DATA_INVALID_OR_CONFLICTING"}


def _summary(rows, horizon):
    observations = [r["markouts"][str(horizon)] for r in rows]
    complete = [m for m in observations if m["status"] == "COMPLETE"]
    values = [Decimal(m["net_return_bps"]) for m in complete]
    return {
        "candidates": len(rows),
        "complete": len(complete),
        "excluded": dict(
            Counter(
                m.get("reason", "UNKNOWN")
                for m in observations
                if m["status"] != "COMPLETE"
            )
        ),
        "mean_net_return_bps": _text(sum(values) / len(values)) if values else None,
        "positive_markout_fraction": _text(
            Decimal(sum(v > 0 for v in values)) / len(values)
        )
        if values
        else None,
        "trade_win_rate": None,
        "strategy_pnl": None,
    }


def _matched(rows, horizon, policy):
    """Greedy non-reused time pairs; not randomized treatment/causal uplift."""
    buckets = defaultdict(list)
    for row in rows:
        if row["post_risk_candidate"] and row["jev"] in {"PASS", "VETO"}:
            # Unknown regimes cannot establish a same-regime comparison.
            if (
                row["regime"] == "UNKNOWN"
                or row["markouts"][str(horizon)]["status"] != "COMPLETE"
            ):
                continue
            day = (row["decided_at_ms"] + 8 * 3_600_000) // DAY
            key = (
                row["side"],
                row["regime"],
                row["source"],
                row["tv"],
                row["config_digest"],
                row["strategy_version"],
                day,
            )
            buckets[key].append(row)
    pairs, unmatched = [], Counter()
    for bucket in buckets.values():
        passing = sorted(
            (r for r in bucket if r["jev"] == "PASS"),
            key=lambda r: (r["decided_at_ms"], r["decision_id"]),
        )
        vetoes = {r["decision_id"]: r for r in bucket if r["jev"] == "VETO"}
        for accepted in passing:
            choices = [
                r
                for r in vetoes.values()
                if abs(r["decided_at_ms"] - accepted["decided_at_ms"])
                <= policy.match_window_minutes * MINUTE
            ]
            if not choices:
                unmatched["PASS"] += 1
                continue
            denied = min(
                choices,
                key=lambda r: (
                    abs(r["decided_at_ms"] - accepted["decided_at_ms"]),
                    r["decided_at_ms"],
                    r["decision_id"],
                ),
            )
            vetoes.pop(denied["decision_id"])
            a = Decimal(accepted["markouts"][str(horizon)]["net_return_bps"])
            b = Decimal(denied["markouts"][str(horizon)]["net_return_bps"])
            pairs.append(
                {
                    "pass_decision_id": accepted["decision_id"],
                    "veto_decision_id": denied["decision_id"],
                    "pass_minus_veto_bps": _text(a - b),
                }
            )
        unmatched["VETO"] += len(vetoes)
    enough = len(pairs) >= policy.minimum_matched_pairs
    return {
        "horizon_minutes": horizon,
        "pairs": pairs,
        "matched_pairs": len(pairs),
        "unmatched_eligible": dict(unmatched),
        "mean_pass_minus_veto_bps": _text(
            sum(Decimal(p["pass_minus_veto_bps"]) for p in pairs) / len(pairs)
        )
        if pairs
        else None,
        "status": "DESCRIPTIVE_ONLY" if enough else "INSUFFICIENT_MATCHED_SAMPLE",
        "causal_improvement_proven": False,
        "matching_dimensions": [
            "direction",
            "regime",
            "source",
            "tv_usage",
            "config_digest",
            "strategy_version",
            "UTC+8_day",
            "bounded_time_distance",
        ],
    }


def build_report(
    records,
    *,
    read_bars,
    start_ms,
    as_of_ms,
    policy=None,
    max_seconds=30,
    truncated=False,
):
    policy = policy or EffectivenessPolicy()
    if (
        type(start_ms) is not int
        or type(as_of_ms) is not int
        or not 0 <= start_ms < as_of_ms
        or type(max_seconds) is not int
        or not 1 <= max_seconds <= 120
    ):
        raise ValueError("EFFECTIVENESS_REPORT_BOUND")
    deadline, seen, rows, excluded = time.monotonic() + max_seconds, {}, [], Counter()
    for record in records:
        candidate = candidate_from_record(record)
        identity, encoded = candidate["decision_id"], canonical(record)
        if identity in seen:
            if seen[identity] != encoded:
                raise ValueError("EFFECTIVENESS_DUPLICATE_DECISION_CONFLICT")
            excluded["DUPLICATE_IDENTICAL_DECISION"] += 1
            continue
        seen[identity] = encoded
        if not start_ms <= candidate["decided_at_ms"] <= as_of_ms:
            excluded["OUTSIDE_AS_OF_WINDOW"] += 1
            continue
        entry = (candidate["decided_at_ms"] // MINUTE + 1) * MINUTE
        completed = [
            h for h in policy.horizons_minutes if entry + h * MINUTE <= as_of_ms
        ]
        bars, failure = [], None
        eligible = candidate["post_risk_candidate"] and candidate["jev"] in {
            "PASS",
            "VETO",
            "FALLBACK",
        }
        if not eligible:
            failure = "NOT_COMPARABLE_POST_RISK_CANDIDATE"
        elif time.monotonic() >= deadline:
            failure = "RESEARCH_TIME_BUDGET_EXHAUSTED"
        elif completed:
            try:
                bars = read_bars(
                    candidate["symbol"], entry, entry + max(completed) * MINUTE
                )
            except Exception:  # noqa: BLE001 - one missing archive never fakes a flat return
                failure = "ARCHIVE_READ_UNAVAILABLE"
        candidate["markouts"] = {}
        for horizon in policy.horizons_minutes:
            value = fixed_horizon_markout(
                candidate,
                bars,
                horizon_minutes=horizon,
                as_of_ms=as_of_ms,
                policy=policy,
            )
            if failure:
                value = {
                    **value,
                    "status": "EXCLUDED",
                    "reason": failure,
                    "net_return_bps": None,
                    "gross_return_bps": None,
                    "gross_mfe_bps": None,
                    "gross_mae_bps": None,
                }
            candidate["markouts"][str(horizon)] = value
        rows.append(candidate)
    groups = defaultdict(list)
    for row in rows:
        groups[(row["jev"], row["tv"], row["side"], row["regime"])].append(row)
    with localcontext() as ctx:
        ctx.prec = 50
        return {
            "schema": "tv-jev-effectiveness-v1",
            "kind": "MARKOUT_NOT_STRATEGY_PNL",
            "as_of_ms": as_of_ms,
            "window_start_ms": start_ms,
            "as_of": datetime.fromtimestamp(
                as_of_ms / 1000, ZoneInfo("Asia/Shanghai")
            ).isoformat(),
            "timezone": "Asia/Shanghai",
            "policy": asdict(policy),
            "truncated": truncated,
            "max_seconds": max_seconds,
            "decision_count": len(rows),
            "excluded_records": dict(excluded),
            "jev_counts": dict(Counter(r["jev"] for r in rows)),
            "tv_counts": dict(Counter(r["tv"] for r in rows)),
            "tv_decision_effect_counts": dict(
                Counter(r["tv_decision_effect"] for r in rows)
            ),
            "actual_filled_count": sum(r["actual_filled"] for r in rows),
            "actual_settled_count": sum(
                r["actual_settled_net_usdt"] is not None for r in rows
            ),
            "actual_settled_net_usdt": str(
                sum(
                    (
                        _number(r["actual_settled_net_usdt"])
                        for r in rows
                        if r["actual_settled_net_usdt"] is not None
                    ),
                    Decimal(0),
                )
            )
            if any(r["actual_settled_net_usdt"] is not None for r in rows)
            else None,
            "groups": [
                {
                    "jev": k[0],
                    "tv": k[1],
                    "side": k[2],
                    "regime": k[3],
                    "horizons": {
                        str(h): _summary(v, h) for h in policy.horizons_minutes
                    },
                }
                for k, v in sorted(groups.items(), key=lambda item: str(item[0]))
            ],
            "matched_jev_comparisons": [
                _matched(rows, h, policy) for h in policy.horizons_minutes
            ],
            "rows": rows,
            "execution_authorized": False,
            "automatic_parameter_updates": False,
            "limitations": [
                "Fixed-horizon delayed-next-minute markouts are not fills, strategy PnL, trade win rates or profit forecasts.",
                "Same fee/slippage assumptions apply to accepted/rejected candidates. Funding, liquidation, leverage and portfolio competition are not simulated.",
                "MFE/MAE are gross minute-bar extrema, not executable prices or proof of intrabar TP/SL order.",
                "Current immutable bar archives do not establish historical ingestion time; outcome bars are never decision inputs.",
                "Only immutable integration decisions in the bounded account window are included; expired signals without decisions are not model vetoes.",
                "Missing, conflicting and incomplete candles stay unknown, not zero return. Recent unclosed horizons are censored.",
                "Matched comparisons control recorded direction/regime/source/version/day only; symbols, candidate quality and selection bias remain confounders.",
                "No alternative historical risk state, capital reuse, order execution or complete strategy exit path is reconstructed.",
                "Minimum pair count is a reporting threshold, not statistical significance or authorization to increase exposure.",
                "Actual settled PnL is reported separately and must never be added to or substituted for hypothetical markouts.",
            ],
        }


def load_records(conn, scope, *, start_ms, as_of_ms, limit=2000):
    """Bounded projection of immutable evidence; requires a read-only transaction."""
    if (
        not isinstance(scope, AccountScope)
        or type(limit) is not int
        or not 1 <= limit <= 10000
    ):
        raise ValueError("EFFECTIVENESS_READ_SCOPE_REQUIRED")
    if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
        raise ValueError("EFFECTIVENESS_READ_ONLY_REQUIRED")
    if (
        type(start_ms) is not int
        or type(as_of_ms) is not int
        or not 0 <= start_ms < as_of_ms
    ):
        raise ValueError("EFFECTIVENESS_CLOCK_INVALID")
    consumers = [
        StrategyScope(**asdict(scope), producer=producer).consumer
        for producer in ("s6", "s8")
    ]
    rows = conn.execute(
        """WITH selected AS MATERIALIZED (
          SELECT d.* FROM v2_strategy_decisions d
          JOIN v2_inbound_signals signal ON signal.signal_id=d.signal_id
          LEFT JOIN v2_signal_receipts receipt ON receipt.consumer=d.consumer AND receipt.signal_id=d.signal_id
          WHERE d.consumer=ANY(%s) AND d.created_at BETWEEN to_timestamp(%s/1000.0) AND to_timestamp(%s/1000.0)
            AND (d.action='OPEN' OR signal.source='tv_bridge' OR left(receipt.reason,4)='JEV_'
              OR receipt.reason='TV_DIRECTION_CONFLICT' OR receipt.signal_id IS NULL)
        )
        SELECT d.decision_id::text,d.signal_id::text,d.evidence_ref,d.action,d.side,
          greatest((e.snapshot->>'decided_at')::bigint,
            (extract(epoch FROM d.created_at)*1000)::bigint) AS decided,
          e.snapshot->>'source',e.snapshot->>'symbol',e.snapshot->>'rationale',
          e.snapshot->'features'->'evaluation',
          e.snapshot->'features'->'context'->'directional'->>'regime',
          e.config->>'strategy',e.config_digest,e.strategy_version,
          EXISTS(SELECT 1 FROM v2_orders o JOIN v2_fills f USING(order_id)
            WHERE o.episode_id=i.intent_id AND o.leg='OPEN'
            AND f.created_at<=to_timestamp(%s/1000.0) AND f.occurred_at_ms<=%s),
          outcome.net_pnl::text
        FROM selected d JOIN v2_decision_evidence e USING(evidence_ref)
        LEFT JOIN v2_trade_intents i ON i.evidence_ref=d.evidence_ref
          AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
        LEFT JOIN v2_directional_outcomes outcome ON outcome.episode_id=i.intent_id
          AND outcome.recorded_at<=to_timestamp(%s/1000.0) AND outcome.closed_at_ms<=%s
        WHERE e.created_at<=to_timestamp(%s/1000.0)
          AND (e.snapshot#>'{features,evaluation,jev_entry_gate}' IS NOT NULL
            OR e.snapshot->>'source'='tv_bridge'
            OR e.snapshot#>'{features,evaluation,signal_fusion}' IS NOT NULL)
        ORDER BY d.created_at DESC,d.decision_id LIMIT %s""",
        (
            consumers,
            start_ms,
            as_of_ms,
            as_of_ms,
            as_of_ms,
            *asdict(scope).values(),
            as_of_ms,
            as_of_ms,
            as_of_ms,
            limit + 1,
        ),
    ).fetchall()
    names = (
        "decision_id",
        "signal_id",
        "evidence_ref",
        "action",
        "side",
        "decided_at_ms",
        "source",
        "symbol",
        "rationale",
        "evaluation",
        "regime",
        "strategy",
        "config_digest",
        "strategy_version",
        "actual_filled",
        "actual_settled_net_usdt",
    )
    return [dict(zip(names, row, strict=True)) for row in rows[:limit]], len(
        rows
    ) > limit


def load_tv_funnel(conn, scope, *, start_ms, as_of_ms):
    """Environment delivery and scoped receipts, deliberately different sets.

    Signals have no account binding at ingress. Lack of this account's receipt
    is not evidence of loss or delayed processing without historical routing.
    Opposite-direction consumers can also legitimately record IGNORED.
    """
    if not isinstance(scope, AccountScope):
        raise TypeError("EFFECTIVENESS_READ_SCOPE_REQUIRED")
    if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
        raise ValueError("EFFECTIVENESS_READ_ONLY_REQUIRED")
    if (
        type(start_ms) is not int
        or type(as_of_ms) is not int
        or not 0 <= start_ms < as_of_ms
    ):
        raise ValueError("EFFECTIVENESS_CLOCK_INVALID")
    consumers = [
        StrategyScope(**asdict(scope), producer=producer).consumer
        for producer in ("s6", "s8")
    ]
    received, symbols, timely, any_receipt, no_receipt_expired = conn.execute(
        """SELECT count(*),count(DISTINCT s.snapshot->>'symbol'),
          count(*) FILTER(WHERE (s.snapshot->>'expires_at_ms')::bigint>
            (extract(epoch FROM s.received_at)*1000)::bigint),
          count(*) FILTER(WHERE EXISTS(SELECT 1 FROM v2_signal_receipts r
            WHERE r.signal_id=s.signal_id AND r.consumer=ANY(%s)
              AND r.completed_at<=to_timestamp(%s/1000.0))),
          count(*) FILTER(WHERE (s.snapshot->>'expires_at_ms')::bigint<=%s
            AND NOT EXISTS(SELECT 1 FROM v2_signal_receipts r
              WHERE r.signal_id=s.signal_id AND r.consumer=ANY(%s)
                AND r.completed_at<=to_timestamp(%s/1000.0)))
        FROM v2_inbound_signals s WHERE s.source='tv_bridge' AND s.environment=%s
          AND s.received_at BETWEEN to_timestamp(%s/1000.0) AND to_timestamp(%s/1000.0)""",
        (
            consumers,
            as_of_ms,
            as_of_ms,
            consumers,
            as_of_ms,
            scope.environment,
            start_ms,
            as_of_ms,
        ),
    ).fetchone()
    rows = conn.execute(
        """SELECT r.consumer,r.outcome,r.reason,count(*)
        FROM v2_signal_receipts r JOIN v2_inbound_signals s USING(signal_id)
        WHERE s.source='tv_bridge' AND s.environment=%s AND r.consumer=ANY(%s)
          AND s.received_at BETWEEN to_timestamp(%s/1000.0) AND to_timestamp(%s/1000.0)
          AND r.completed_at<=to_timestamp(%s/1000.0)
        GROUP BY r.consumer,r.outcome,r.reason ORDER BY count(*) DESC LIMIT 101""",
        (scope.environment, consumers, start_ms, as_of_ms, as_of_ms),
    ).fetchall()
    return {
        "environment_received_signals": received,
        "environment_symbols": symbols,
        "environment_arrived_before_expiry": timely,
        "with_any_scoped_account_receipt": any_receipt,
        "expired_without_scoped_account_receipt": no_receipt_expired,
        "account_receipt_groups": [
            {
                "producer": "s6" if r[0] == consumers[0] else "s8",
                "outcome": r[1],
                "reason": r[2],
                "count": r[3],
            }
            for r in rows[:100]
        ],
        "receipt_groups_truncated": len(rows) > 100,
        "limitations": [
            "Ingress is environment-scoped, not account-scoped; historical route applicability is not reconstructed.",
            "Receipt groups can overlap by signal across S6/S8. Opposite-direction ignores are not failed eligible entries.",
            "Expired without this account's receipt is not attributed to queue lag, account outage or TradingView without historical route/task evidence.",
        ],
    }
