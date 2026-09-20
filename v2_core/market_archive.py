"""Content-addressed ClickHouse market payloads, separate from PG business state."""

import re

from v2_core.evidence import digest


class ArchiveIntegrityError(ValueError):
    """Confirmed content mismatch, distinct from transport unavailability."""


class ClickHouseCandleArchive:
    def __init__(self, client):
        self.client = client

    def put(self, content_digest, encoded):
        if (
            not isinstance(encoded, str)
            or len(encoded.encode()) > 8_000_000
            or digest(encoded) != content_digest
        ):
            raise ValueError("archive payload digest mismatch")
        self.client.insert(
            "v2_candle_archive",
            [[content_digest, encoded]],
            column_names=["content_digest", "payload"],
        )
        # Confirm read visibility before PG makes this content discoverable.
        return self.get(content_digest) == encoded

    def get(self, content_digest):
        if not isinstance(content_digest, str) or not re.fullmatch(
            "[0-9a-f]{64}", content_digest
        ):
            raise ValueError("invalid archive digest")
        rows = self.client.query(
            "SELECT payload FROM v2_candle_archive WHERE content_digest={digest:String} LIMIT 1",
            parameters={"digest": content_digest},
        ).result_rows
        if not rows:
            return None
        encoded = rows[0][0]
        if (
            not isinstance(encoded, str)
            or len(encoded.encode()) > 8_000_000
            or digest(encoded) != content_digest
        ):
            raise ArchiveIntegrityError("stored archive digest mismatch")
        return encoded
