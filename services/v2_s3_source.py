"""PG source delivery ledger with content-addressed external market archive."""

import json
from uuid import uuid4

from services.v2_s3_candles import build_frame
from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds
from v2_core.market_archive import ArchiveIntegrityError
from v2_core.signals import SignalConflict


class SourceQuarantined(ValueError):
    """Audited terminal input failure; supervisor must alert without raw payload."""


def frame_digest(frame):
    return digest(
        canonical(
            {
                "contexts": frame["windows"],
                "raw_windows": frame["raw_windows"],
                "removed": [],
                "observed_at": frame["observed_at"],
            }
        )
    )


class DurableCandleSource:
    def __init__(self, publisher, *, archive, lease_seconds=60):
        if (
            publisher.source != "s3"
            or type(lease_seconds) is not int
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("S3 publisher and bounded source lease required")
        self.publisher, self.archive, self.lease_seconds = (
            publisher,
            archive,
            lease_seconds,
        )
        self.pending = None  # Delivery token only; all progress is authoritative in PG.

    def enqueue(self, batch):
        encoded = canonical(batch)
        if len(encoded.encode()) > 8_000_000:
            raise ValueError("archive batch exceeds size budget")
        frozen = json.loads(encoded)
        frame = build_frame(frozen, environment=self.publisher.environment)
        content, expected = digest(encoded), frame_digest(frame)
        # Archive confirmation precedes PG discovery. A crash here can leave an
        # unreferenced content-addressed object, never a falsely acknowledged row.
        if self.archive.put(content, encoded) is not True:
            raise RuntimeError("archive write not confirmed")
        with self.publisher._connect() as conn:
            self.publisher.lock(conn)
            committed = conn.execute(
                "SELECT input_digest FROM v2_s3_frames WHERE environment=%s AND frame_id=%s",
                (self.publisher.environment, frame["frame_id"]),
            ).fetchone()
            if committed is not None and committed != (expected,):
                raise SignalConflict("published source frame content conflict")
            conn.execute(
                """INSERT INTO v2_candle_deliveries(environment,frame_id,observed_at_ms,
                expires_at_ms,archive_digest,input_digest) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING""",
                (
                    self.publisher.environment,
                    frame["frame_id"],
                    frame["observed_at"],
                    frame["observed_at"] + self.publisher.lifetime_ms,
                    content,
                    expected,
                ),
            )
            row = conn.execute(
                "SELECT archive_digest,input_digest FROM v2_candle_deliveries WHERE environment=%s AND frame_id=%s",
                (self.publisher.environment, frame["frame_id"]),
            ).fetchone()
            if row != (content, expected):
                raise SignalConflict("source frame content conflict")
        return frame["frame_id"]

    def _audit(self, conn, frame_id, outcome):
        conn.execute(
            """INSERT INTO v2_candle_delivery_events(event_id,environment,frame_id,outcome)
                     VALUES (%s,%s,%s,%s)""",
            (str(uuid4()), self.publisher.environment, frame_id, outcome),
        )

    def read(self):
        self.pending = None
        quarantined = False
        with self.publisher._connect() as conn:
            self.publisher.lock(conn)
            row = conn.execute(
                """SELECT frame_id,archive_digest,input_digest,observed_at_ms,expires_at_ms,
                COALESCE(lease_until>clock_timestamp(),FALSE) FROM v2_candle_deliveries
                WHERE environment=%s AND status='PENDING' ORDER BY observed_at_ms,frame_id LIMIT 1 FOR UPDATE""",
                (self.publisher.environment,),
            ).fetchone()
            if row is None or row[5]:
                return None
            frame_id, archived, expected, observed, expires, _ = row
            now = milliseconds(self.publisher.clock_ms())
            if observed > now:
                return None
            committed = conn.execute(
                "SELECT input_digest FROM v2_s3_frames WHERE environment=%s AND frame_id=%s",
                (self.publisher.environment, frame_id),
            ).fetchone()
            newer = conn.execute(
                "SELECT 1 FROM v2_producer_batches WHERE source='s3' AND environment=%s AND observed_at_ms>=%s AND frame_id<>%s LIMIT 1",
                (self.publisher.environment, observed, frame_id),
            ).fetchone()
            conflict = committed is not None and committed != (expected,)
            if conflict or (
                committed is None
                and (
                    newer
                    or now >= expires
                    or now - observed > self.publisher.max_age_ms
                )
            ):
                conn.execute(
                    """UPDATE v2_candle_deliveries SET status='QUARANTINED',lease_token=NULL,lease_until=NULL
                              WHERE environment=%s AND frame_id=%s""",
                    (self.publisher.environment, frame_id),
                )
                self._audit(
                    conn,
                    frame_id,
                    "PUBLISHED_INPUT_CONFLICT"
                    if conflict
                    else "SUPERSEDED_UNPUBLISHED"
                    if newer
                    else "EXPIRED_UNPUBLISHED",
                )
                quarantined = True
            else:
                token = str(uuid4())
                conn.execute(
                    """UPDATE v2_candle_deliveries SET lease_token=%s,lease_until=clock_timestamp()+make_interval(secs=>%s),
                              attempts=attempts+1 WHERE environment=%s AND frame_id=%s""",
                    (token, self.lease_seconds, self.publisher.environment, frame_id),
                )
        if quarantined:
            raise SourceQuarantined("source frame quarantined")
        self.pending = (frame_id, token, expected)
        try:
            encoded = self.archive.get(archived)
        except ArchiveIntegrityError as exc:
            self._quarantine("ARCHIVE_CORRUPT")
            raise SourceQuarantined("archive integrity failure") from exc
        if encoded is None:
            raise RuntimeError("archive unavailable")
        try:
            if (
                not isinstance(encoded, str)
                or len(encoded.encode()) > 8_000_000
                or digest(encoded) != archived
            ):
                raise ValueError("archive digest conflict")
            batch = json.loads(encoded)
            frame = build_frame(batch, environment=self.publisher.environment)
            if frame["frame_id"] != frame_id or frame_digest(frame) != expected:
                raise ValueError("archive frame conflict")
        except (ValueError, TypeError, KeyError) as exc:
            self._quarantine("ARCHIVE_CORRUPT")
            raise SourceQuarantined("archive integrity failure") from exc
        return batch

    def _quarantine(self, reason):
        frame_id, token, _ = self.pending
        with self.publisher._connect() as conn:
            row = conn.execute(
                """UPDATE v2_candle_deliveries SET status='QUARANTINED',lease_token=NULL,lease_until=NULL
                WHERE environment=%s AND frame_id=%s AND status='PENDING' AND lease_token=%s
                AND lease_until>clock_timestamp() RETURNING frame_id""",
                (self.publisher.environment, frame_id, token),
            ).fetchone()
            if row:
                self._audit(conn, frame_id, reason)

    def ack(self, frame_id):
        if self.pending is None or self.pending[0] != frame_id:
            return False
        _, token, expected = self.pending
        with self.publisher._connect() as conn:
            self.publisher.lock(conn)
            committed = conn.execute(
                "SELECT input_digest FROM v2_s3_frames WHERE environment=%s AND frame_id=%s",
                (self.publisher.environment, frame_id),
            ).fetchone()
            if committed != (expected,):
                return False
            row = conn.execute(
                """UPDATE v2_candle_deliveries SET status='ACKNOWLEDGED',lease_token=NULL,lease_until=NULL
                WHERE environment=%s AND frame_id=%s AND status='PENDING' AND lease_token=%s
                AND lease_until>clock_timestamp() RETURNING frame_id""",
                (self.publisher.environment, frame_id, token),
            ).fetchone()
            if row:
                self._audit(conn, frame_id, "ACKNOWLEDGED")
        return row is not None
