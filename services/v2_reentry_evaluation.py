"""Read-only outcomes AFTER frozen shadow judgements; never feeds admission.

Actual original-trade labels are not simulated returns or avoided losses.
Bounded, scoped primary-key lookups; no venue calls or new storage privileges.
"""

from collections import Counter, defaultdict
from dataclasses import asdict
from decimal import Decimal, InvalidOperation, localcontext
from uuid import UUID

from psycopg.types.json import Jsonb

from v2_core.strategy import StrategyScope


def _readonly(conn):
    if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
        raise ValueError("SHADOW_READ_ONLY_REQUIRED")


def _consumers(scope):
    return [StrategyScope(**asdict(scope), producer=p).consumer for p in ("s6", "s8")]


def coverage_report(conn, registry, scope, *, health, now):
    """Recent persisted OPEN decisions vs observations, not all-time coverage.

    May include decisions from before a route change within this short window.
    Absence is a diagnostic gap, not proof that the observer owed that decision.
    """
    _readonly(conn)
    result = {
        "status": "UNAVAILABLE",
        "window_ms": 300000,
        "sample_limit": 500,
        "truncated": False,
        "timing_basis": "DECISION_DATABASE_CREATED_AT",
    }
    if (
        health.get("registry_id") != registry
        or health.get("stale")
        or not health.get("policy_digest")
        or not health.get("started_ms")
    ):
        return result
    limits = health.get("limits", {})
    lag = limits.get("max_lag_ms", 120000)
    if type(lag) is not int or not 1000 <= lag <= 300000:
        return result
    start = max(health["started_ms"], now - result["window_ms"])
    rows = conn.execute(
        """WITH candidates AS MATERIALIZED (
        SELECT decision_id,created_at FROM v2_strategy_decisions
        WHERE consumer=ANY(%s) AND action='OPEN'
        AND created_at BETWEEN to_timestamp(%s/1000.0) AND to_timestamp(%s/1000.0)
        ORDER BY created_at DESC,decision_id LIMIT 501)
      SELECT (extract(epoch FROM d.created_at)*1000)::bigint, EXISTS(
        SELECT 1 FROM v2_reentry_observations o WHERE o.decision_id=d.decision_id
          AND o.policy_digest=%s AND o.registry_id=%s AND o.scope=%s AND o.observed_ms<=%s)
      FROM candidates d ORDER BY d.created_at DESC,d.decision_id""",
        (
            _consumers(scope),
            start,
            now,
            health["policy_digest"],
            registry,
            Jsonb(asdict(scope)),
            now,
        ),
    ).fetchall()
    sample = rows[:500]
    recorded = sum(r[1] for r in sample)
    overdue = sum(not r[1] and now - r[0] > lag for r in sample)
    result.update(
        status="NO_NEW_OPEN" if not rows else "RECENT_WINDOW",
        window_start_ms=start,
        window_end_ms=now,
        policy_digest=health["policy_digest"],
        decisions=len(sample),
        recorded=recorded,
        unobserved_over_lag=overdue,
        pending=len(sample) - recorded - overdue,
        truncated=len(rows) > 500,
    )
    return result


def outcomes(conn, scope, decision_ids, *, now):
    _readonly(conn)
    if not isinstance(decision_ids, (list, tuple)) or len(decision_ids) > 100:
        raise ValueError("SHADOW_OUTCOME_LIMIT")
    ids = sorted({str(UUID(x)) for x in decision_ids})
    if not ids:
        return {}
    # Current strategy contract uses the same UUID for the OPEN decision/intent.
    # Also require consumer scope, signal and evidence equality: never guess by symbol/time.
    rows = conn.execute(
        """SELECT d.decision_id::text,i.intent_id::text,
      f.oq::text,f.cq::text,f.closed_ms,ord.terminal,ord.known,ord.maintenance,
      s.revision,(extract(epoch FROM s.settled_at)*1000)::bigint,
      s.evidence->>'net_pnl',s.evidence->'exchange_flat',s.evidence->'orders_terminal',
      s.evidence->'fills_complete',s.evidence->'cash_complete',
      (extract(epoch FROM ord.latest_update)*1000)::bigint, f.latest_known_ms,
      ep.accounting_revision,ep.status,s.evidence->'ledger_revision'
      FROM v2_strategy_decisions d
      LEFT JOIN v2_trade_intents i ON i.intent_id=d.decision_id
        AND i.signal_id=d.signal_id AND i.evidence_ref=d.evidence_ref
        AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
        AND i.producer IN ('s6','s8') AND i.created_at<=to_timestamp(%s/1000.0)
      LEFT JOIN v2_episodes ep ON ep.episode_id=i.intent_id
      LEFT JOIN LATERAL (SELECT
        coalesce(sum(z.quantity) FILTER(WHERE o.leg='OPEN'),0) oq,
        coalesce(sum(z.quantity) FILTER(WHERE o.leg='CLOSE'),0) cq,
        max(z.occurred_at_ms) FILTER(WHERE o.leg='CLOSE') closed_ms,
        (extract(epoch FROM max(z.created_at))*1000)::bigint latest_known_ms
        FROM v2_orders o JOIN v2_fills z USING(order_id) WHERE o.episode_id=i.intent_id
        AND z.occurred_at_ms<=%s AND z.created_at<=to_timestamp(%s/1000.0)) f ON true
      LEFT JOIN LATERAL (SELECT
        bool_and(o.status IN ('FILLED','CANCELLED','REJECTED','RECONCILED')) terminal,
        bool_and(o.updated_at<=to_timestamp(%s/1000.0)) known,
        bool_or(o.request_evidence->>'origin'='APPROVED_TESTNET_MAINTENANCE') maintenance,
        max(o.updated_at) latest_update FROM v2_orders o WHERE o.episode_id=i.intent_id) ord ON true
      LEFT JOIN LATERAL (SELECT revision,settled_at,evidence FROM v2_settlements
        WHERE episode_id=i.intent_id AND currency='USDT' AND settled_at<=to_timestamp(%s/1000.0)
        ORDER BY revision DESC LIMIT 1) s ON true
      WHERE d.decision_id=ANY(%s::uuid[]) AND d.consumer=ANY(%s) AND d.action='OPEN'
        AND d.created_at<=to_timestamp(%s/1000.0)""",
        (*asdict(scope).values(), now, now, now, now, now, ids, _consumers(scope), now),
    ).fetchall()
    result = {key: {"state": "UNLINKED_DECISION", "net_pnl": None} for key in ids}
    for r in rows:
        (
            identity,
            intent,
            oq,
            cq,
            closed,
            terminal,
            known,
            maintenance,
            revision,
            settled,
            net,
            flat,
            orders_done,
            fills_done,
            cash_done,
            order_updated,
            fill_known,
            current_revision,
            episode_status,
            proof_revision,
        ) = r
        value = {
            "state": "NO_INTENT",
            "net_pnl": None,
            "intent_id": intent,
            "closed_ms": closed,
            "settlement_revision": revision,
            "settled_ms": settled,
        }
        if intent:
            opening, closing = Decimal(oq), Decimal(cq)
            if maintenance:
                value["state"] = "MAINTENANCE_EXCLUDED"
            elif known is False:
                value["state"] = "LATER_ORDER_STATE"
            elif closing > opening or opening < 0 or closing < 0:
                value["state"] = "INCONSISTENT_FILLS"
            elif opening == 0:
                value["state"] = "UNFILLED"
            elif opening > closing:
                value["state"] = "OPEN_OR_PARTIAL_EXIT"
            elif terminal is not True:
                value["state"] = "AWAITING_TERMINAL_ORDERS"
            elif revision is None:
                value["state"] = "AWAITING_SETTLEMENT"
            elif any(
                flag is not True for flag in (flat, orders_done, fills_done, cash_done)
            ) or settled < max(closed or 0, order_updated or 0, fill_known or 0):
                value["state"] = "SETTLEMENT_EVIDENCE_INCOMPLETE"
            else:
                try:
                    if not isinstance(net, str) or len(net) > 128:
                        raise ValueError("NET_INVALID")
                    number = Decimal(net)
                    if not number.is_finite() or (
                        number and not -38 <= number.adjusted() <= 38
                    ):
                        raise ValueError("NET_INVALID")
                    if (
                        revision != current_revision
                        or episode_status != "SETTLED"
                        or type(proof_revision) is not int
                        or proof_revision != revision
                    ):
                        value["state"] = "SETTLEMENT_REVISION_CHANGED"
                    else:
                        value.update(state="SETTLED", net_pnl=str(number))
                except (InvalidOperation, TypeError, ValueError):
                    value["state"] = "SETTLEMENT_EVIDENCE_INCOMPLETE"
        result[identity] = value
    return result


def evaluation_summary(rows):
    """One original outcome per decision AND policy, never pooling policy versions."""
    groups = defaultdict(list)
    seen = set()
    duplicate_rows = 0
    for row in rows:
        identity = (row["policy_digest"], row["decision_id"])
        if identity in seen:
            duplicate_rows += 1
            continue
        seen.add(identity)
        key = tuple(
            row.get(k) or "UNKNOWN"
            for k in (
                "policy_digest",
                "status",
                "claim",
                "source",
                "side",
                "config_digest",
                "strategy_version",
            )
        )
        groups[key].append(row)
    result = []
    with localcontext() as context:
        context.prec = 80
        for key, items in sorted(groups.items()):
            states = Counter(
                r.get("actual", {}).get("state", "UNAVAILABLE") for r in items
            )
            settled = [
                Decimal(r["actual"]["net_pnl"])
                for r in items
                if r.get("actual", {}).get("state") == "SETTLED"
            ]
            wins = sum(v > 0 for v in settled)
            losses = sum(v < 0 for v in settled)
            result.append(
                {
                    **dict(
                        zip(
                            (
                                "policy_digest",
                                "status",
                                "claim",
                                "source",
                                "side",
                                "config_digest",
                                "strategy_version",
                            ),
                            key,
                        )
                    ),
                    "observations": len(items),
                    "outcome_states": dict(states),
                    "settled": len(settled),
                    "wins": wins,
                    "losses": losses,
                    "breakeven": len(settled) - wins - losses,
                    "actual_net_pnl": str(sum(settled, Decimal(0)))
                    if settled
                    else None,
                    "actual_win_rate_pct": str(Decimal(wins) * 100 / len(settled))
                    if settled
                    else None,
                    "wait_with_actual_profit": wins if key[1] == "WAIT" else 0,
                    "ready_with_actual_loss": losses if key[1] == "READY" else 0,
                }
            )
    return {
        "kind": "ORIGINAL_TRADE_LABELS_NOT_COUNTERFACTUAL",
        "groups": result,
        "duplicate_observations_excluded": duplicate_rows,
        "execution_authorized": False,
        "simulated_pnl": None,
        "automated_promotion": False,
    }
