"""Audited historical-rate valuation, distinct from actual cash conversion."""

import json
from uuid import UUID

from v2_core.evidence import canonical
from v2_core.ledger import amount, emit


class Valuations:
    def __init__(self, connection_factory):
        self._connect = connection_factory

    def record(
        self,
        episode_id,
        *,
        kind,
        fact_key,
        target_currency,
        rate,
        quote_at_ms,
        max_distance_ms,
        expected_version,
        request_key,
        evidence,
    ):
        episode_id = str(UUID(episode_id))
        rate = amount(rate, positive=True)
        if kind not in {"FEE", "CASH"} or target_currency not in {"USDT", "USDC"}:
            raise ValueError("unsupported valuation")
        if (
            type(quote_at_ms) is not int
            or quote_at_ms < 0
            or type(max_distance_ms) is not int
            or not 0 <= max_distance_ms <= 300000
            or type(expected_version) is not int
            or expected_version < 0
        ):
            raise ValueError("bounded historical quote and version required")
        if not isinstance(request_key, str) or not request_key.strip():
            raise ValueError("valuation idempotency key required")
        if not isinstance(evidence, dict) or any(
            not isinstance(evidence.get(k), str) or not evidence[k].strip()
            for k in ("source", "reason")
        ):
            raise ValueError("valuation evidence source and reason required")
        encoded = canonical(
            {
                "method": "HISTORICAL_MARK",
                "max_distance_ms": max_distance_ms,
                "proof": evidence,
            }
        )
        with self._connect() as conn:
            if not conn.execute(
                "SELECT 1 FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                (episode_id,),
            ).fetchone():
                raise ValueError("unknown intent")
            if kind == "FEE":
                fact = conn.execute(
                    """SELECT f.fee_currency,f.occurred_at_ms FROM v2_fills f
                    JOIN v2_orders o USING(order_id) WHERE o.episode_id=%s AND f.fill_key=%s""",
                    (episode_id, fact_key),
                ).fetchone()
            else:
                fact = conn.execute(
                    "SELECT currency,occurred_at_ms FROM v2_cash_adjustments WHERE episode_id=%s AND adjustment_key=%s",
                    (episode_id, fact_key),
                ).fetchone()
            if fact is None or fact[0] == target_currency:
                raise ValueError("foreign-currency fact in this episode required")
            if abs(quote_at_ms - fact[1]) > max_distance_ms:
                raise ValueError("quote too distant from accounting fact")
            values = (
                kind,
                fact_key,
                target_currency,
                expected_version + 1,
                rate,
                quote_at_ms,
                json.loads(encoded),
            )
            previous = conn.execute(
                """SELECT fact_kind,fact_key,target_currency,version,rate,quote_at_ms,evidence
                FROM v2_fx_valuations WHERE episode_id=%s AND request_key=%s""",
                (episode_id, request_key),
            ).fetchone()
            if previous is not None:
                if previous != values:
                    raise ValueError("valuation idempotency conflict")
                return False
            latest = conn.execute(
                """SELECT COALESCE(max(version),0) FROM v2_fx_valuations
                WHERE fact_kind=%s AND fact_key=%s AND target_currency=%s""",
                (kind, fact_key, target_currency),
            ).fetchone()[0]
            if latest != expected_version:
                raise ValueError("stale valuation version")
            conn.execute(
                """INSERT INTO v2_fx_valuations(episode_id,request_key,fill_key,adjustment_key,
                source_currency,target_currency,version,rate,quote_at_ms,evidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (
                    episode_id,
                    request_key,
                    fact_key if kind == "FEE" else None,
                    fact_key if kind == "CASH" else None,
                    fact[0],
                    target_currency,
                    expected_version + 1,
                    rate,
                    quote_at_ms,
                    encoded,
                ),
            )
            conn.execute(
                "UPDATE v2_episodes SET accounting_revision=accounting_revision+1 WHERE episode_id=%s",
                (episode_id,),
            )
            emit(
                conn,
                episode_id,
                "VALUATION:" + request_key,
                {
                    "kind": kind,
                    "fact_key": fact_key,
                    "source_currency": fact[0],
                    "target_currency": target_currency,
                    "rate": str(rate),
                    "version": expected_version + 1,
                    "quote_at_ms": quote_at_ms,
                    "evidence": json.loads(encoded),
                },
            )
        return True
