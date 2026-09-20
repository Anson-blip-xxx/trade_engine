from types import SimpleNamespace

import pytest

from v2_core.evidence import digest
from v2_core.market_archive import ArchiveIntegrityError, ClickHouseCandleArchive


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
