"""Durable normalized signal inbox. Redis notifications are only wake-up hints."""

import json
from uuid import UUID, uuid5

from v2_core.evidence import canonical
from v2_core.state import normalized

_NAMESPACE = UUID("335484b5-02ae-4e01-8fbf-c2b5703264d3")


class SignalConflict(ValueError):
    """An existing source identity cannot be reused for different facts."""


def signal_consumer(intent):
    return json.dumps(
        [
            intent.exchange,
            intent.account_id,
            intent.environment,
            intent.product,
            intent.producer,
        ],
        separators=(",", ":"),
    )


class Signals:
    def __init__(self, connection_factory):
        self._connect = connection_factory

    def lookup(self, *, source, environment, request_key):
        for value in (source, environment, request_key):
            normalized(value)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT signal_id::text,snapshot FROM v2_inbound_signals
                WHERE source=%s AND environment=%s AND request_key=%s""",
                (source, environment, request_key),
            ).fetchone()
        return None if row is None else {"signal_id": row[0], "snapshot": row[1]}

    def admit(self, *, source, environment, request_key, snapshot):
        for value in (source, environment, request_key):
            normalized(value)
        encoded = canonical(snapshot)
        required = {"observed_at", "expires_at_ms", "symbol", "signal", "features"}
        if not required <= snapshot.keys() or not snapshot.keys() <= required | {
            "rationale"
        }:
            raise ValueError(
                "normalized signal fields required; raw webhook secrets must not be stored"
            )
        if (
            type(snapshot["observed_at"]) is not int
            or type(snapshot["expires_at_ms"]) is not int
            or not 0 <= snapshot["observed_at"] < snapshot["expires_at_ms"]
        ):
            raise ValueError("invalid signal timestamps")
        normalized(snapshot["symbol"])
        normalized(snapshot["signal"])
        if (
            not isinstance(snapshot["features"], dict)
            or len(encoded.encode()) > 1_000_000
        ):
            raise ValueError("bounded feature object required")
        identity = str(
            uuid5(_NAMESPACE, json.dumps([source, environment, request_key]))
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO v2_inbound_signals(signal_id,source,environment,request_key,snapshot)
                VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING""",
                (identity, source, environment, request_key, encoded),
            )
            existing = conn.execute(
                "SELECT source,environment,request_key,snapshot FROM v2_inbound_signals WHERE signal_id=%s",
                (identity,),
            ).fetchone()
            if existing != (source, environment, request_key, json.loads(encoded)):
                raise SignalConflict("signal identity content conflict")
        return identity

    def pending(self, *, consumer, environment, source, limit=100):
        for value in (consumer, environment, source):
            normalized(value)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid limit")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT signal_id::text,request_key,snapshot
                FROM v2_inbound_signals s WHERE environment=%s AND source=%s
                AND NOT EXISTS (SELECT 1 FROM v2_signal_receipts r
                    WHERE r.consumer=%s AND r.signal_id=s.signal_id)
                ORDER BY received_at,signal_id LIMIT %s""",
                (environment, source, consumer, limit),
            ).fetchall()
        return [
            dict(zip(("signal_id", "request_key", "snapshot"), row, strict=True))
            for row in rows
        ]

    def complete(self, *, consumer, signal_id, outcome, reason, intent_id=None):
        normalized(consumer)
        normalized(reason)
        signal_id = str(UUID(signal_id))
        if outcome not in {"INTENT", "IGNORED", "EXPIRED"} or (outcome == "INTENT") != (
            intent_id is not None
        ):
            raise ValueError("explicit signal outcome required")
        if intent_id is not None:
            intent_id = str(UUID(intent_id))
        with self._connect() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM v2_inbound_signals WHERE signal_id=%s FOR UPDATE",
                    (signal_id,),
                ).fetchone()
                is None
            ):
                raise ValueError("unknown signal")
            if outcome == "INTENT":
                link = conn.execute(
                    """SELECT 1 FROM v2_trade_intents i
                    JOIN v2_decision_evidence e USING(evidence_ref)
                    JOIN v2_inbound_signals s ON s.signal_id=%s
                    WHERE i.intent_id=%s AND i.environment=s.environment
                      AND i.payload->>'symbol'=s.snapshot->>'symbol'
                      AND e.snapshot->>'signal_id'=s.signal_id::text""",
                    (signal_id, intent_id),
                ).fetchone()
                if link is None:
                    raise ValueError(
                        "intent must reference this signal's decision evidence"
                    )
            inserted = conn.execute(
                """INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,intent_id,reason)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING signal_id""",
                (consumer, signal_id, outcome, intent_id, reason),
            ).fetchone()
            existing = conn.execute(
                "SELECT outcome,intent_id::text,reason FROM v2_signal_receipts WHERE consumer=%s AND signal_id=%s",
                (consumer, signal_id),
            ).fetchone()
            if existing != (outcome, intent_id, reason):
                raise ValueError("signal outcome conflict")
        return inserted is not None
