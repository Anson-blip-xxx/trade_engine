"""Bounded SANDBOX re-entry audit; no orders, venue calls, writes or promotion.

V2_REENTRY_AUDIT_DSN is an environment-only read DSN. --research-root identifies
local research sources; their hashes are included for reproducibility.
The report contains aggregates, never account, order, episode or decision IDs.
"""

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo


def finite_amount(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("SETTLEMENT_AMOUNT_INVALID")
    try:
        amount = Decimal(value)
    except ArithmeticError as exc:
        raise ValueError("SETTLEMENT_AMOUNT_INVALID") from exc
    if not amount.is_finite() or (amount and not -38 <= amount.adjusted() <= 38):
        raise ValueError("SETTLEMENT_AMOUNT_INVALID")
    return amount


def summarize(rows, outcomes):
    """Actual USDT settlement labels, not hypothetical prevented losses."""
    states = [outcomes[row["id"]] for row in rows]
    settled = [row for row in rows if outcomes[row["id"]]["state"] == "SETTLED"]
    with localcontext() as ctx:
        ctx.prec = 80
        values = [finite_amount(outcomes[row["id"]]["net_pnl"]) for row in settled]
        fees = [
            finite_amount(row["fees_usdt"])
            for row in settled
            if row.get("fees_usdt") is not None
        ]
        return {
            "entries": len(rows),
            "outcome_states": dict(Counter(item["state"] for item in states)),
            "settled": len(values),
            "wins": sum(value > 0 for value in values),
            "losses": sum(value < 0 for value in values),
            "actual_net_pnl_usdt": str(sum(values, Decimal(0))) if values else None,
            "known_recorded_fees_usdt": str(sum(fees, Decimal(0))) if fees else None,
            "fees_known_settlements": len(fees),
            "net_already_includes_settlement_costs": True,
        }


def timing_audit(rows, *, recompute, digest):
    """Audit frozen evidence event time and availability time separately."""
    counts = Counter()
    lags = []
    for row in rows:
        payload, decided, observed, intent_created = (
            row["payload"],
            row["decided_ms"],
            row["observed_ms"],
            row["intent_created_ms"],
        )
        counts["observations"] += 1
        candidate = payload.get("candidate", {})
        previous = payload.get("previous")
        bars = payload.get("bars", [])
        assessment = payload.get("assessment", {})
        completed = payload.get("collection_completed_ms")
        lags.append(observed - decided)
        counts["observer_started_after_decision"] += observed > decided
        counts["completion_timestamp_missing"] += type(completed) is not int
        if type(completed) is int:
            counts["completed_after_decision"] += completed > decided
            counts["completed_before_or_at_decision"] += completed <= decided
            counts["completed_before_or_at_intent_persist"] += (
                type(intent_created) is int and completed <= intent_created
            )
        counts["archive_arrival_at_decision_proven"] += (
            payload.get("archive_arrival_at_decision_proven") is True
        )
        counts["candidate_clock_mismatch"] += candidate.get("decided_ms") != decided
        counts["input_digest_mismatch"] += payload.get("inputs_digest") != digest(
            {"candidate": candidate, "previous": previous, "bars": bars}
        )
        if bars:
            width = payload["policy"]["timeframe_minutes"] * 60000
            counts["with_archived_bars"] += 1
            counts["future_or_unclosed_bar"] += any(
                type(bar.get("t")) is not int or bar["t"] + width > decided
                for bar in bars
            )
        if previous:
            counts["with_frozen_prior_close"] += 1
            counts["prior_exit_known_after_decision"] += (
                previous["exit_known_ms"] > decided
            )
        if previous and bars:
            counts["recomputed_assessments"] += 1
            try:
                counts["recomputed_assessment_mismatch"] += (
                    recompute(payload) != assessment
                )
            except (ValueError, TypeError, KeyError):
                counts["recomputed_assessment_invalid"] += 1
    result = dict(counts)
    result["minimum_observer_start_lag_ms"] = min(lags) if lags else None
    result["maximum_observer_start_lag_ms"] = max(lags) if lags else None
    return result


def collect(conn, *, days, research_root):
    # Imports are deliberately from operator-selected local research sources,
    # never from arbitrary network content or the trading daemon's runtime.
    if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on" or conn.execute(
        "SHOW transaction_isolation"
    ).fetchone()[0] not in {"repeatable read", "serializable"}:
        raise ValueError("READ_ONLY_REPEATABLE_SNAPSHOT_REQUIRED")
    sys.path.insert(0, str(research_root))
    from psycopg.types.json import Jsonb

    from services.v2_reentry_evaluation import outcomes
    from services.v2_reentry_observer import active_route, digest
    from services.v2_trade_review import trade_review
    from v2_core.account_risk import AccountScope
    from v2_core.quant_indicators import Bar
    from v2_core.reentry import ReentryPolicy, assess_reentry

    route = active_route(conn)
    if not route or route["scope"]["environment"] != "SANDBOX":
        raise ValueError("UNIQUE_ACTIVE_SANDBOX_REQUIRED")
    scope = AccountScope(**route["scope"])
    now = conn.execute(
        "SELECT (extract(epoch FROM clock_timestamp())*1000)::bigint"
    ).fetchone()[0]
    review = trade_review(conn, scope, days=days, now_ms=now, limit=500)
    trades = [row for row in review["trades"] if not row["maintenance"]]
    observations = conn.execute(
        """SELECT o.decision_id::text,o.decided_ms,o.observed_ms,o.status,o.reason,
          o.side,o.claim,o.policy_digest,o.payload,
          (extract(epoch FROM i.created_at)*1000)::bigint
        FROM v2_reentry_observations o
        LEFT JOIN v2_trade_intents i ON i.intent_id=o.decision_id
          AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
        WHERE o.registry_id=%s AND o.scope=%s
          AND o.observed_ms BETWEEN %s AND %s
        ORDER BY o.observed_ms DESC,o.decision_id LIMIT 501""",
        (
            *(
                route["scope"][key]
                for key in ("exchange", "account_id", "environment", "product")
            ),
            route["registry_id"],
            Jsonb(route["scope"]),
            now - days * 86400000,
            now,
        ),
    ).fetchall()
    columns = (
        "id",
        "decided_ms",
        "observed_ms",
        "status",
        "reason",
        "side",
        "claim",
        "policy_digest",
        "payload",
        "intent_created_ms",
    )
    observations_truncated = len(observations) > 500
    observations = [dict(zip(columns, row)) for row in observations[:500]]
    identities = list(dict.fromkeys(row["id"] for row in trades + observations))
    labels = {}
    for offset in range(0, len(identities), 100):
        labels.update(outcomes(conn, scope, identities[offset : offset + 100], now=now))
    by_id = {row["id"]: row for row in trades}
    reentries = [
        row
        for row in trades
        if by_id.get(row["prior_episode_id"], {}).get("side") == row["side"]
    ]
    group_rows = defaultdict(list)
    for row in observations:
        candidate = row["payload"].get("candidate", {})
        key = (
            row["status"],
            row["reason"],
            row["side"],
            row["claim"],
            row["policy_digest"],
            candidate.get("config_digest", "UNKNOWN"),
            candidate.get("strategy_version", "UNKNOWN"),
            candidate.get("source", "UNKNOWN"),
        )
        group_rows[key].append({**row, "fees_usdt": None})

    def recompute(payload):
        return assess_reentry(
            bars=[Bar(**bar) for bar in payload["bars"]],
            side=payload["candidate"]["side"],
            signal_observed_ms=int(payload["candidate"]["signal_observed_ms"]),
            as_of_ms=payload["candidate"]["decided_ms"],
            policy=ReentryPolicy(**payload["policy"]),
            **payload["previous"],
        )

    timing = timing_audit(observations, recompute=recompute, digest=digest)
    # Compare selected prior episodes only where the current bounded factual
    # reader still knows the same historical interval. Late ledger revisions may
    # make this inconclusive; absence is not silently accepted as a match.
    prior_comparison = Counter()
    for row in observations:
        previous = row["payload"].get("previous")
        actual = by_id.get(row["id"])
        if not previous or actual is None:
            continue
        prior_comparison["compared"] += 1
        if actual["prior_episode_id"] is None:
            prior_comparison["current_bounded_history_unknown"] += 1
        elif previous["previous_episode"] == actual["prior_episode_id"]:
            prior_comparison["same_previous_episode"] += 1
        else:
            prior_comparison["different_previous_episode_requires_review"] += 1
    files = (
        "services/v2_reentry_observer.py",
        "services/v2_trade_review.py",
        "services/v2_reentry_evaluation.py",
        "v2_core/reentry.py",
    )
    return {
        "kind": "SCOPED_REENTRY_FACTUAL_ASSOCIATION_AND_CAUSALITY_AUDIT",
        "as_of_utc8": datetime.fromtimestamp(
            now / 1000, ZoneInfo("Asia/Shanghai")
        ).isoformat(),
        "environment": "SANDBOX",
        "window_days": days,
        "snapshot_clock": "POSTGRESQL_CLOCK_TIMESTAMP_FIXED_ONCE",
        "gap_time_basis": "INTENT_CREATED_AT_MINUS_PREVIOUS_ACTUAL_FINAL_CLOSE_FILL",
        "gap_is_actual_new_open_fill_time": False,
        "scope_selection": "UNIQUE_ACTIVE_SANDBOX_ROUTE_NOT_AN_ACCOUNT_SWITCH",
        "actual_trades_truncated": review["truncated"],
        "observer_rows_truncated": observations_truncated,
        "first_observed_ms": min(
            (r["observed_ms"] for r in observations), default=None
        ),
        "overall": summarize(trades, labels),
        "same_side_reentries": summarize(reentries, labels),
        "prior_context_coverage": {
            "filled_entries": len(trades),
            "same_side_prior_in_audited_window": len(reentries),
            "no_prior_observed_in_bounded_history": sum(
                row["prior_episode_id"] is None for row in trades
            ),
            "prior_outside_audited_entries_or_maintenance_excluded": sum(
                row["prior_episode_id"] is not None
                and row["prior_episode_id"] not in by_id
                for row in trades
            ),
            "opposite_side_prior_in_audited_window": sum(
                row["prior_episode_id"] in by_id
                and by_id[row["prior_episode_id"]]["side"] != row["side"]
                for row in trades
            ),
            "missing_prior_is_not_first_entry_proof": True,
        },
        "descriptive_gap_groups_not_proposed_cooldowns": {
            name: summarize(
                [row for row in reentries if low <= row["prior_close_gap_ms"] < high],
                labels,
            )
            for name, low, high in (
                ("under_5m", 0, 300000),
                ("5m_to_30m", 300000, 1800000),
                ("30m_to_2h", 1800000, 7200000),
                ("over_2h", 7200000, 2**63),
            )
        },
        "observer_groups": [
            {
                **dict(
                    zip(
                        (
                            "status",
                            "reason",
                            "side",
                            "claim",
                            "policy_digest",
                            "config_digest",
                            "strategy_version",
                            "source",
                        ),
                        key,
                    )
                ),
                **summarize(rows, labels),
            }
            for key, rows in sorted(group_rows.items())
        ],
        "rapid_reentry_observer_overlap": {
            "actual_reentries_under_5m": sum(
                row["prior_close_gap_ms"] < 300000 for row in reentries
            ),
            "frozen_observations": [
                {
                    "symbol": actual["symbol"],
                    "side": actual["side"],
                    "gap_ms": actual["prior_close_gap_ms"],
                    "observer_status": observation["status"],
                    "observer_reason": observation["reason"],
                    "observer_policy_digest": observation["policy_digest"],
                    "actual_outcome_state": labels[actual["id"]]["state"],
                    "actual_net_pnl_usdt": labels[actual["id"]]["net_pnl"],
                    "completed_before_or_at_decision": (
                        observation["payload"]["collection_completed_ms"]
                        <= observation["decided_ms"]
                        if type(observation["payload"].get("collection_completed_ms"))
                        is int
                        else None
                    ),
                    "archive_arrival_at_decision_proven": observation["payload"].get(
                        "archive_arrival_at_decision_proven"
                    )
                    is True,
                }
                for actual in reentries
                if actual["prior_close_gap_ms"] < 300000
                for observation in observations
                if observation["id"] == actual["id"]
            ],
        },
        "timing_and_frozen_input_audit": timing,
        "prior_episode_comparison": dict(prior_comparison),
        "research_source_sha256": {
            name: hashlib.sha256((research_root / name).read_bytes()).hexdigest()
            for name in files
        },
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "counterfactual_pnl": None,
        "limitations": [
            "USDT unit is established by currency='USDT' settlement evidence, not the symbol suffix.",
            "Net labels require revision-matching complete settlements; open or unknown outcomes are not zero.",
            "Gap groups are descriptive, not optimized thresholds or independent observations.",
            "Gap uses new intent database creation minus prior final close exchange fill time, not new opening fill minus prior fill.",
            "Prior episodes outside the audited entry window remain unknown to the same-side subset; missing context is not a non-reentry label.",
            "Observer verdicts completed after the decision are retrospective labels, not available entry-time permits.",
            "Closed historical bars do not prove archive arrival before entry; this is not an executable counterfactual backtest.",
            "No replacement entries, portfolio competition, hypothetical exits or prevented-loss totals are simulated.",
            "Frozen comparison groups retain side, strategy, config, policy and source; do not promote from pooled labels.",
            "Existing factual reader bounds 500 entries plus 24-hour prior context; missing prior context is unknown.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--active-sandbox", action="store_true", required=True)
    parser.add_argument("--days", type=int, choices=(1, 7, 14, 30, 90), default=14)
    parser.add_argument("--research-root", type=Path, required=True)
    args = parser.parse_args()
    research_root = args.research_root.resolve(strict=True)
    if not research_root.is_dir():
        raise ValueError("RESEARCH_ROOT_REQUIRED")
    dsn = os.environ.get("V2_REENTRY_AUDIT_DSN")
    if not dsn:
        raise ValueError("READ_DSN_REQUIRED")
    import psycopg

    with psycopg.connect(dsn, options="-c default_transaction_read_only=on") as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET LOCAL statement_timeout='5s'")
        conn.execute("SET LOCAL lock_timeout='100ms'")
        conn.execute("SET LOCAL search_path=trade_v2,pg_catalog")
        report = collect(conn, days=args.days, research_root=research_root)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - never expose connection or scope details
        raise SystemExit("REENTRY_READONLY_AUDIT_UNAVAILABLE") from None
