from types import SimpleNamespace

import pytest
from market_fakes import ArchiveClient, candle_batch

from v2_core.chunked_archive import ClickHouseChunkedArchive
from v2_core.evidence import canonical, digest
from v2_core.market_archive import (
    ArchiveIntegrityError,
    ClickHouseCandleArchive,
    decoded_digest,
)


@pytest.mark.parametrize(
    "bad", [b"a" * 63, b"a" * 64 + b"\x00", b"\xff" * 64, "A" * 64, None, 123]
)
def test_fixed_string_digest_never_trims_or_ignores_bad_bytes(bad):
    with pytest.raises(ArchiveIntegrityError):
        decoded_digest(bad)


def test_chunked_archive_roundtrip_with_real_driver_fixedstring_representation():
    class BytesClient(ArchiveClient):
        def query(self, sql, parameters):
            result = super().query(sql, parameters)
            if "keys" in parameters:
                result.result_rows = [
                    (key.encode("ascii"), value) for key, value in result.result_rows
                ]
            elif "v2_candle_manifests" in sql:
                result.result_rows = [
                    (value, key.encode("ascii")) for value, key in result.result_rows
                ]
            return result

    archive = ClickHouseChunkedArchive(BytesClient())
    encoded = canonical(candle_batch())
    assert archive.put(digest(encoded), encoded)
    assert archive.get(digest(encoded)) == encoded


def test_archive_insert_and_parameterized_verified_read():
    calls = []
    payload = '{"candles":{}}'
    key = digest(payload)

    def insert(table, rows, column_names):
        calls.append((table, rows, column_names))

    def query(sql, parameters):
        assert "{digest:String}" in sql and parameters == {"digest": key}
        return SimpleNamespace(result_rows=[[payload]])

    archive = ClickHouseCandleArchive(SimpleNamespace(insert=insert, query=query))
    assert archive.put(key, payload) is True
    assert calls == [
        ("v2_candle_archive", [[key, payload]], ["content_digest", "payload"])
    ]
    assert archive.get(key) == payload


@pytest.mark.parametrize("payload", ["bad", None, 10])
def test_archive_corruption_is_distinct_from_transport_failure(payload):
    archive = ClickHouseCandleArchive(
        SimpleNamespace(query=lambda *a, **k: SimpleNamespace(result_rows=[[payload]]))
    )
    with pytest.raises(ArchiveIntegrityError):
        archive.get("a" * 64)


def test_archive_missing_and_invalid_requests():
    archive = ClickHouseCandleArchive(
        SimpleNamespace(query=lambda *a, **k: SimpleNamespace(result_rows=[]))
    )
    assert archive.get("a" * 64) is None
    with pytest.raises(ValueError):
        archive.get("invalid'")
    with pytest.raises(ValueError):
        archive.put("a" * 64, "{}")


def test_archive_unconfirmed_visibility_cannot_acknowledge_write():
    archive = ClickHouseCandleArchive(
        SimpleNamespace(
            insert=lambda *a, **k: None,
            query=lambda *a, **k: SimpleNamespace(result_rows=[]),
        )
    )
    assert archive.put(digest("{}"), "{}") is False
