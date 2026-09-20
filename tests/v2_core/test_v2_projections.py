import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from v2_core.projections import RedisProjection


def test_real_redis_rejects_stale_and_conflicting_projections():
    socket = os.environ.get("V2_REDIS_TEST_SOCKET")
    if not socket or os.environ.get("V2_CORE_TEST_ISOLATED") != "YES":
        pytest.skip("requires isolated Redis unix socket")
    import redis

    client = redis.Redis(unix_socket_path=socket, decode_responses=True)
    namespace = "v2:test:" + uuid4().hex
    projection = RedisProjection(client, namespace)
    try:
        assert projection.put("entity", 2**60, {"status": "NEWER"})
        assert projection.put("entity", 2**60 - 1, {"status": "OLD"})
        assert not projection.put("entity", 2**60, {"status": "CONFLICT"})
        assert projection.put("entity", 2**60, {"status": "NEWER"})
        assert client.hget(namespace + ":entity", "payload") == '{"status":"NEWER"}'
    finally:
        client.delete(namespace + ":entity")
        client.close()


def test_clickhouse_logical_view_deduplicates_before_background_merge(tmp_path):
    if os.environ.get("V2_CORE_TEST_ISOLATED") != "YES":
        pytest.skip("requires explicit isolated local ClickHouse QA")
    ddl = (
        Path(__file__).resolve().parents[2] / "db/clickhouse_v2_schema.sql"
    ).read_text()
    # Stop background merges to demonstrate query-time, not eventual, correctness.
    query = (
        ddl
        + """
        SYSTEM STOP MERGES v2_trade_events;
        INSERT INTO v2_trade_events VALUES
            ('e1','i1','INTENT_ACCEPTED','{}',repeat('a',64)),
            ('e1','i1','INTENT_ACCEPTED','{}',repeat('a',64));
        SELECT count() FROM v2_trade_events_logical;
    """
    )
    result = subprocess.run(
        [
            "clickhouse",
            "local",
            "--path",
            str(tmp_path / "ch"),
            "--background_schedule_pool_size",
            "2",
            "--max_threads",
            "2",
            "--multiquery",
            "--query",
            query,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_clickhouse_candle_archive_survives_duplicate_delivery(tmp_path):
    if os.environ.get("V2_CORE_TEST_ISOLATED") != "YES":
        pytest.skip("requires isolated ClickHouse QA")
    ddl = (
        Path(__file__).resolve().parents[2] / "db/clickhouse_v2_schema.sql"
    ).read_text()
    query = (
        ddl
        + """
        INSERT INTO v2_candle_archive VALUES (lower(hex(SHA256('{}'))),'{}'),(lower(hex(SHA256('{}'))),'{}');
        SELECT payload FROM v2_candle_archive WHERE content_digest=lower(hex(SHA256('{}'))) LIMIT 1;
        SELECT count() FROM v2_candle_archive FINAL;
        INSERT INTO v2_candle_manifests VALUES (repeat('a',64),'{}',lower(hex(SHA256('{}'))));
        SELECT payload,manifest_digest=lower(hex(SHA256(payload))) FROM v2_candle_manifests WHERE batch_digest=repeat('a',64) LIMIT 1;
        SELECT any(payload) FROM v2_candle_archive WHERE content_digest IN [lower(hex(SHA256('{}')))] GROUP BY content_digest;
    """
    )
    result = subprocess.run(
        [
            "clickhouse",
            "local",
            "--path",
            str(tmp_path / "archive"),
            "--background_schedule_pool_size",
            "2",
            "--max_threads",
            "2",
            "--multiquery",
            "--query",
            query,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["{}", "1", "{}\t1", "{}"]
