"""Bounded public-flow research checkpoints, independent of trading state.

The caller decides whether a checkpoint is complete and when to retry it. This
store preserves the supplied observations, enforces a shared retry ceiling, and
rejects damaged evidence rather than silently treating it as a cache miss.
"""

import hmac
from datetime import date, datetime, time, timedelta, timezone

from psycopg.types.json import Jsonb

from v2_core.evidence import canonical, digest

VERSION = "mover-flow-public-v1"
SOURCE = "BINANCE_LIVE_PUBLIC"
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_ATTEMPTS = 3
MAX_PRUNE_BATCH = 500
_MAX_MS = 2**53 - 1
_DAY_MS = 86_400_000
_ZONE = timezone(timedelta(hours=8))
_ROW_LIMITS = {"spot": 24, "open_interest": 30, "funding": 200}
_COMPONENT_SOURCES = {
    "spot": "BINANCE_SPOT_LIVE",
    "open_interest": "BINANCE_USDM_LIVE",
    "funding": "BINANCE_USDM_LIVE",
}
_SELECT = """SELECT payload,attempts,complete,
    floor(extract(epoch FROM updated_at)*1000)::bigint,input_digest
    FROM v2_mover_flow_evidence
    WHERE day=%s AND symbol=%s AND method_version=%s"""


def _window(day):
    if not isinstance(day, str):
        raise ValueError("FLOW_EVIDENCE_DAY")  # noqa: TRY004 - stable validation code
    try:
        parsed = date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError("FLOW_EVIDENCE_DAY") from exc
    if parsed.isoformat() != day:
        raise ValueError("FLOW_EVIDENCE_DAY")
    start = int(datetime.combine(parsed, time(), _ZONE).timestamp()) * 1000
    if not 0 <= start < start + _DAY_MS <= _MAX_MS:
        raise ValueError("FLOW_EVIDENCE_DAY")
    return start, start + _DAY_MS


def _identity(day, symbol):
    window = _window(day)
    # Public names can be Unicode; their eligibility for engine execution is a
    # separate question. Match the daily mover research universe's symbol rule.
    if (
        not isinstance(symbol, str)
        or not 5 <= len(symbol) <= 44
        or not symbol.endswith("USDT")
        or not all(char.isalnum() or char == "_" for char in symbol[:-4])
    ):
        raise ValueError("FLOW_EVIDENCE_SYMBOL")
    return window


def _payload(day, symbol, payload):
    start, end = _identity(day, symbol)
    if not isinstance(payload, dict):
        raise ValueError("FLOW_EVIDENCE_PAYLOAD")  # noqa: TRY004 - stable validation code
    if (
        payload.get("version") != VERSION
        or payload.get("source") != SOURCE
        or payload.get("symbol") != symbol
        or type(payload.get("start_ms")) is not int
        or type(payload.get("end_ms")) is not int
        or payload["start_ms"] != start
        or payload["end_ms"] != end
    ):
        raise ValueError("FLOW_EVIDENCE_IDENTITY")
    mapping = payload.get("mapping")
    if not isinstance(mapping, dict) or mapping.get("status") not in {
        "MATCHED",
        "UNKNOWN",
    }:
        raise ValueError("FLOW_EVIDENCE_MAPPING")
    for component, maximum in _ROW_LIMITS.items():
        envelope = payload.get(component)
        if (
            not isinstance(envelope, dict)
            or envelope.get("source") != _COMPONENT_SOURCES[component]
            or envelope.get("status") not in {"OBSERVED", "UNKNOWN"}
            or type(envelope.get("fetched_at_ms")) is not int
            or not 0 <= envelope["fetched_at_ms"] <= _MAX_MS
            or not isinstance(envelope.get("rows"), list)
            or len(envelope["rows"]) > maximum
        ):
            raise ValueError("FLOW_EVIDENCE_ENVELOPE")
    encoded = canonical(payload)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("FLOW_EVIDENCE_SIZE")
    return encoded


def _record(day, symbol, row):
    if row is None:
        return None
    try:
        payload, attempts, complete, updated_at_ms, input_digest = row
        encoded = _payload(day, symbol, payload)
        if (
            type(attempts) is not int
            or not 1 <= attempts <= MAX_ATTEMPTS
            or type(complete) is not bool
            or type(updated_at_ms) is not int
            or updated_at_ms < 0
            or not isinstance(input_digest, str)
            or not hmac.compare_digest(input_digest, digest(encoded))
        ):
            raise ValueError("checkpoint metadata or digest mismatch")
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ValueError("FLOW_EVIDENCE_CORRUPT") from exc
    return {
        "payload": payload,
        "attempts": attempts,
        "complete": complete,
        "updated_at_ms": updated_at_ms,
        "input_digest": input_digest,
    }


class FlowEvidenceStore:
    def __init__(self, connect):
        self.connect = connect

    def read(self, day, symbol):
        _identity(day, symbol)
        with self.connect() as conn:
            row = conn.execute(_SELECT, (day, symbol, VERSION)).fetchone()
            return _record(day, symbol, row)

    def save(self, day, symbol, payload, *, complete):
        if type(complete) is not bool:
            raise ValueError("FLOW_EVIDENCE_COMPLETE")
        encoded = _payload(day, symbol, payload)
        with self.connect() as conn:
            # Lock even an absent key so corruption cannot be overwritten by a
            # racing retry. The SQL guard independently enforces the retry cap.
            key = f"mover-flow-evidence:{VERSION}:{day}:{symbol}"
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
            prior = _record(
                day, symbol, conn.execute(_SELECT, (day, symbol, VERSION)).fetchone()
            )
            if prior is not None and (
                prior["complete"] or prior["attempts"] >= MAX_ATTEMPTS
            ):
                return prior
            row = conn.execute(
                """INSERT INTO v2_mover_flow_evidence
                (day,symbol,method_version,payload,input_digest,complete)
                VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT(day,symbol,method_version) DO UPDATE SET
                  payload=excluded.payload,input_digest=excluded.input_digest,
                  complete=excluded.complete,
                  attempts=v2_mover_flow_evidence.attempts+1,
                  updated_at=clock_timestamp()
                WHERE NOT v2_mover_flow_evidence.complete
                  AND v2_mover_flow_evidence.attempts<3
                RETURNING payload,attempts,complete,
                  floor(extract(epoch FROM updated_at)*1000)::bigint,input_digest""",
                (day, symbol, VERSION, Jsonb(payload), digest(encoded), complete),
            ).fetchone()
            if row is None:
                row = conn.execute(_SELECT, (day, symbol, VERSION)).fetchone()
            result = _record(day, symbol, row)
            if result is None:
                raise ValueError("FLOW_EVIDENCE_WRITE_MISSING")
            return result

    def prune_raw(self, completed_day, *, limit=MAX_PRUNE_BATCH):
        """Delete at most ``limit`` checkpoints older than 30 completed days.

        The caller supplies an already completed UTC+8 date. Compact immutable
        reports are stored elsewhere and are never read or changed here.
        """
        _window(completed_day)
        if type(limit) is not int or not 1 <= limit <= MAX_PRUNE_BATCH:
            raise ValueError("FLOW_EVIDENCE_PRUNE_LIMIT")
        with self.connect() as conn:
            return conn.execute(
                """WITH expired AS (
                  SELECT day,symbol,method_version FROM v2_mover_flow_evidence
                  WHERE day<%s::date-30 AND method_version=%s
                  ORDER BY day,symbol LIMIT %s FOR UPDATE SKIP LOCKED
                ) DELETE FROM v2_mover_flow_evidence AS evidence USING expired
                WHERE (evidence.day,evidence.symbol,evidence.method_version)=
                  (expired.day,expired.symbol,expired.method_version)""",
                (completed_day, VERSION, limit),
            ).rowcount
