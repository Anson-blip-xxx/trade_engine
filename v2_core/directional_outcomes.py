"""Immutable settlement-derived directional outcomes and 14-day rollups."""

import json
from dataclasses import asdict
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from uuid import UUID

from psycopg.types.json import Jsonb

from v2_core.account_risk import AccountScope
from v2_core.directional import analysis_adjustment
from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds, symbol
from v2_core.ledger import amount


def exact(value):
    with localcontext() as ctx:
        ctx.prec = 100
        rounded = value.quantize(
            Decimal("0.000000000000000001"), rounding=ROUND_HALF_EVEN
        )
    return format(rounded.normalize(), "f")


def settlement_amount(value):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("bounded settlement decimal required")
    try:
        parsed = Decimal(value)
        normalized = Decimal(exact(parsed))
    except Exception as exc:
        raise ValueError("invalid settlement decimal") from exc
    if (
        not parsed.is_finite()
        or parsed.copy_abs() >= Decimal("1e20")
        or parsed != normalized
    ):
        raise ValueError("settlement decimal loses NUMERIC precision")
    return normalized


class DirectionalOutcomeJournal:
    def __init__(self, connect, *, scope):
        if not isinstance(scope, AccountScope):
            raise TypeError("typed account scope required")
        self.connect, self.scope = connect, scope

    def record(self, episode):
        episode = str(UUID(episode))
        with self.connect() as conn:
            row = conn.execute(
                """SELECT i.producer,i.payload->>'symbol',e.snapshot,s.revision,s.evidence,
                max(f.occurred_at_ms) FILTER (WHERE o.leg='CLOSE'),
                sum(f.quantity*f.price) FILTER (WHERE o.leg='OPEN'),
                sum(f.quantity) FILTER (WHERE o.leg='OPEN'),
                sum(f.quantity*f.price) FILTER (WHERE o.leg='CLOSE'),
                sum(f.quantity) FILTER (WHERE o.leg='CLOSE')
                FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref)
                JOIN v2_settlements s ON s.episode_id=i.intent_id
                JOIN v2_orders o ON o.episode_id=i.intent_id JOIN v2_fills f USING(order_id)
                WHERE i.intent_id=%s AND
                (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                GROUP BY i.producer,i.payload,e.snapshot,s.revision,s.evidence""",
                (episode, *asdict(self.scope).values()),
            ).fetchone()
            if row is None or row[0] not in {"s6", "s8"}:
                raise ValueError("SETTLED_DIRECTIONAL_EPISODE_REQUIRED")
            (
                producer,
                target,
                snapshot,
                revision,
                settlement,
                closed,
                opened,
                opening_quantity,
                closing,
                quantity,
            ) = row
            evaluation = snapshot["features"]["evaluation"]
            plan, sizing = evaluation["market_plan"], evaluation["sizing"]
            if (
                plan["strategy"].lower() != producer
                or symbol(target) != snapshot["symbol"]
                or sizing["reason"] != "SIZED"
                or amount(sizing["notional"], positive=True) <= 0
                or amount(sizing["quantity"], positive=True) != opening_quantity
                or opening_quantity != quantity
                or closed is None
                or quantity is None
                or quantity <= 0
            ):
                raise ValueError("DIRECTIONAL_DECISION_FACT_MISMATCH")
            if type(plan["score"]) is not int or not 0 <= plan["score"] <= 100:
                raise ValueError("DIRECTIONAL_QUALITY_SCORE_INVALID")
            score = Decimal(plan["score"])
            net = settlement_amount(settlement["net_pnl"])
            with localcontext() as ctx:
                ctx.prec = 100
                return_pct = net / opened * 100
                close_price = closing / quantity
            evidence = {
                "source": "directional-outcome-v1",
                "episode_id": episode,
                "decision_evidence_ref": digest(canonical({"snapshot": snapshot})),
                "settlement_digest": digest(canonical({"settlement": settlement})),
                "planned_notional": sizing["notional"],
                "opening_notional": exact(opened),
                "closing_quantity": exact(quantity),
            }
            values = (
                episode,
                *asdict(self.scope).values(),
                producer,
                target,
                plan["event_type"],
                closed,
                revision,
                exact(net),
                exact(return_pct),
                exact(score),
                exact(close_price),
                Jsonb(evidence),
            )
            conn.execute(
                """INSERT INTO v2_directional_outcomes(
                episode_id,exchange,account_id,environment,product,producer,symbol,event_type,
                closed_at_ms,settlement_revision,net_pnl,return_pct,quality_score,closing_price,evidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING""",
                values,
            )
            saved = conn.execute(
                """SELECT episode_id::text,exchange,account_id,environment,product,producer,symbol,
                event_type,closed_at_ms,settlement_revision,net_pnl,return_pct,
                quality_score,closing_price,evidence
                FROM v2_directional_outcomes WHERE episode_id=%s""",
                (episode,),
            ).fetchone()
            expected = (
                episode,
                *asdict(self.scope).values(),
                producer,
                target,
                plan["event_type"],
                closed,
                revision,
                Decimal(exact(net)),
                Decimal(exact(return_pct)),
                Decimal(exact(score)),
                Decimal(exact(close_price)),
                evidence,
            )
            if saved != expected:
                raise ValueError("DIRECTIONAL_OUTCOME_CONFLICT")
            return {
                "episode_id": episode,
                "closed_at_ms": closed,
                "return_pct": exact(return_pct),
                "quality_score": exact(score),
                "closing_price": exact(close_price),
            }

    def record_t60(self, episode, *, observed_at_ms, price, evidence):
        episode, observed = str(UUID(episode)), milliseconds(observed_at_ms)
        follow_price = amount(price, positive=True)
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("T60_MARKET_EVIDENCE_REQUIRED")
        with self.connect() as conn:
            outcome = conn.execute(
                """SELECT producer,closed_at_ms,closing_price FROM v2_directional_outcomes
                WHERE episode_id=%s""",
                (episode,),
            ).fetchone()
            if (
                outcome is None
                or not outcome[1] + 3600000 <= observed <= outcome[1] + 3900000
            ):
                raise ValueError("T60_OBSERVATION_WINDOW")
            with localcontext() as ctx:
                ctx.prec = 100
                change = (follow_price - outcome[2]) / outcome[2] * 100
                value = change if outcome[0] == "s6" else -change
            frozen = json.loads(canonical(evidence))
            conn.execute(
                """INSERT INTO v2_directional_followups(episode_id,horizon_minutes,
                observed_at_ms,price,post_close_return_pct,evidence)
                VALUES (%s,60,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (episode, observed, exact(follow_price), exact(value), Jsonb(frozen)),
            )
            saved = conn.execute(
                """SELECT observed_at_ms,price,post_close_return_pct,evidence
                FROM v2_directional_followups WHERE episode_id=%s AND horizon_minutes=60""",
                (episode,),
            ).fetchone()
            expected = (
                observed,
                Decimal(exact(follow_price)),
                Decimal(exact(value)),
                frozen,
            )
            if saved != expected:
                raise ValueError("DIRECTIONAL_T60_CONFLICT")
            return {"episode_id": episode, "t60_return_pct": exact(value)}


class DirectionalHistory:
    def __init__(self, connect, *, scope, producer, clock_ms, max_age_ms=120000):
        if (
            not isinstance(scope, AccountScope)
            or producer not in {"s6", "s8"}
            or not callable(clock_ms)
            or type(max_age_ms) is not int
            or not 1000 <= max_age_ms <= 600000
        ):
            raise ValueError("explicit directional history scope required")
        self.connect, self.scope, self.producer = connect, scope, producer
        self.clock, self.max_age = clock_ms, max_age_ms

    def __call__(self, signal):
        now, target = milliseconds(self.clock()), symbol(signal["symbol"])
        event = signal["signal"]
        start = max(0, now - 14 * 86400000)
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT o.episode_id::text,o.closed_at_ms,o.net_pnl,o.return_pct,
                o.quality_score,f.post_close_return_pct FROM v2_directional_outcomes o
                LEFT JOIN v2_directional_followups f ON f.episode_id=o.episode_id
                AND f.horizon_minutes=60 WHERE
                (o.exchange,o.account_id,o.environment,o.product)=(%s,%s,%s,%s)
                AND o.producer=%s AND o.symbol=%s AND o.event_type=%s
                AND o.closed_at_ms>=%s AND o.closed_at_ms<=%s
                ORDER BY o.closed_at_ms,o.episode_id""",
                (
                    *asdict(self.scope).values(),
                    self.producer,
                    target,
                    event,
                    start,
                    now,
                ),
            ).fetchall()
        if any(
            closed + 3900000 < now and follow is None for _, closed, *_, follow in rows
        ):
            raise ValueError("DIRECTIONAL_T60_INCOMPLETE")
        trades = len(rows)
        wins = sum(net > 0 for _, _, net, *_ in rows)

        def average(index, selected=rows):
            return (
                Decimal(0)
                if not selected
                else sum(row[index] for row in selected) / len(selected)
            )

        followed = [row for row in rows if row[5] is not None]
        stats = {
            "trades": trades,
            "win_rate": exact(
                Decimal(0) if not trades else Decimal(wins) / trades * 100
            ),
            "avg_quality_score": exact(average(4)),
            "t60_avg_post_close_return_pct": exact(average(5, followed)),
            "avg_pct": exact(average(3)),
        }
        analysis_adjustment(stats, mode="hard")
        evidence = {
            "source": "pg-directional-history-v1",
            "account_scope": asdict(self.scope),
            "producer": self.producer,
            "symbol": target,
            "event_type": event,
            "lookback_start_ms": start,
            "lookback_end_ms": now,
            "episode_ids": [row[0] for row in rows],
            "t60_count": len(followed),
            "rollup_digest": digest(canonical({"stats": stats})),
        }
        return {
            "stats": stats,
            "evidence": evidence,
            "observed_at_ms": now,
            "valid_until_ms": now + self.max_age,
        }
