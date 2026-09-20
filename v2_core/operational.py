"""Durable, bounded operational notifications; never authorizes trading.

Transient errors coalesce by DB five-minute bucket, not process memory. Quarantine
alerts are reconstructed from immutable evidence after crashes. No local fallback.
The external sink must use finite timeouts; duplicate delivery remains possible.
"""

import re
from uuid import uuid4

from psycopg.types.json import Jsonb

from v2_core.delivery import Projector


class OperationalProjector(Projector):
    _outbox = "v2_operational_outbox"
    _receipts = "v2_operational_receipts"
    _attempts = "v2_operational_attempts"
    _subject = "scope_id"


class MarketAlerts:
    def __init__(self, connect, *, environment, notify):
        if environment not in ("SANDBOX", "LIVE") or not callable(notify):
            raise ValueError("bound environment and notification sink required")
        self._connect, self.environment = connect, environment
        self.projector = OperationalProjector(
            connect,
            "market-alerts:" + environment,
            lambda event_id, scope, kind, payload: (
                notify(
                    {
                        "event_id": event_id,
                        "environment": scope,
                        "kind": kind,
                        **payload,
                    }
                )
                if scope == environment
                else False
            ),
        )
        self.projector._outbox = {
            "SANDBOX": "v2_operational_sandbox_outbox",
            "LIVE": "v2_operational_live_outbox",
        }[environment]

    def record(self, result):
        stage, error = result.get("stage"), result.get("error_code")
        if stage == "PROJECT" and error is None:
            error = "ProjectionUnconfirmed"
        if stage not in {
            "RECOVER",
            "COLLECT",
            "ENQUEUE",
            "PROCESS",
            "READ",
            "VALIDATE",
            "PUBLISH",
            "PROJECT",
            "ACK",
        }:
            raise ValueError("invalid market alert stage")
        if not isinstance(error, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z_0-9]{0,79}", error
        ):
            raise ValueError("bounded error class required")
        # Ignore all other fields: exception messages, headers and credentials are
        # not a notification contract. DB time survives application clock changes.
        with self._connect() as conn:
            bucket = conn.execute(
                "SELECT floor(extract(epoch FROM clock_timestamp())/300)::bigint"
            ).fetchone()[0]
            key = f"failure:{stage}:{error}:{bucket}"
            conn.execute(
                """INSERT INTO v2_operational_outbox(event_id,scope_id,dedup_key,event_type,payload)
                VALUES (%s,%s,%s,'MARKET_FAILURE',%s) ON CONFLICT(scope_id,dedup_key) DO NOTHING""",
                (
                    str(uuid4()),
                    self.environment,
                    key,
                    Jsonb({"stage": stage, "error_code": error, "bucket": bucket}),
                ),
            )
        return True

    def scan_quarantines(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("bounded scan required")
        with self._connect() as conn:
            rows = conn.execute(
                """INSERT INTO v2_operational_outbox(event_id,scope_id,dedup_key,event_type,payload)
                SELECT e.event_id,e.environment,'quarantine:'||e.event_id::text,'CANDLE_QUARANTINED',
                    jsonb_build_object('frame_id',e.frame_id,'outcome',e.outcome,
                        'evidence_event_id',e.event_id::text,'occurred_at',e.created_at)
                FROM v2_candle_delivery_events e
                WHERE e.environment=%s AND e.outcome<>'ACKNOWLEDGED'
                  AND NOT EXISTS (SELECT 1 FROM v2_operational_outbox o
                    WHERE o.scope_id=e.environment AND o.dedup_key='quarantine:'||e.event_id::text)
                ORDER BY e.created_at,e.event_id LIMIT %s ON CONFLICT DO NOTHING
                RETURNING event_id""",
                (self.environment, limit),
            ).fetchall()
        return len(rows)

    def flush(self, limit=10):
        self.scan_quarantines(limit)
        return self.projector.run_scheduled_batch(limit)
