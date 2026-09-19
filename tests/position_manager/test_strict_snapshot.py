"""Strict typed pm:positions snapshot CAS against isolated Redis."""

import shutil
import subprocess
import time

import pytest
import redis

from position_state.strict_snapshot import (
    PM_POSITIONS_KEY,
    StrictRedisPositionSnapshotAdapter,
    StrictSnapshotRead,
    StrictSnapshotReadCode,
    StrictSnapshotWriteCode,
)


@pytest.fixture(scope="module")
def redis_client(tmp_path_factory):
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is not installed")
    directory = tmp_path_factory.mktemp("strict-snapshot-redis")
    socket = directory / "redis.sock"
    process = subprocess.Popen(
        [
            executable,
            "--port",
            "0",
            "--unixsocket",
            str(socket),
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(directory),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    client = redis.Redis(unix_socket_path=str(socket))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if client.ping():
                break
        except redis.RedisError:
            time.sleep(0.02)
    else:
        process.terminate()
        pytest.fail("isolated Redis did not start")
    yield client
    client.close()
    process.terminate()
    process.wait(timeout=3)


@pytest.fixture
def adapter(redis_client):
    redis_client.flushdb()
    return StrictRedisPositionSnapshotAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )


def _snapshot(qty=2):
    return {"BTCUSDT": {"entry": 100, "qty": qty, "side": "LONG"}}


def test_missing_read_can_create_with_typed_ack(adapter):
    read = adapter.read()
    assert read.code is StrictSnapshotReadCode.NOT_FOUND
    result = adapter.compare_and_set(read, _snapshot())
    assert result.code is StrictSnapshotWriteCode.APPLIED
    assert result.positions == _snapshot()
    assert adapter.read().positions == _snapshot()


def test_found_read_can_compare_and_set(adapter):
    assert adapter.compare_and_set(adapter.read(), _snapshot()).applied
    expected = adapter.read()
    result = adapter.compare_and_set(expected, _snapshot(1))
    assert result.code is StrictSnapshotWriteCode.APPLIED
    assert result.positions == _snapshot(1)


def test_concurrent_snapshot_change_returns_stale(adapter, redis_client):
    assert adapter.compare_and_set(adapter.read(), _snapshot()).applied
    expected = adapter.read()
    redis_client.set(PM_POSITIONS_KEY, '{"ETHUSDT":{"qty":3}}')
    result = adapter.compare_and_set(expected, _snapshot(1))
    assert result.code is StrictSnapshotWriteCode.STALE
    assert result.positions == {"ETHUSDT": {"qty": 3}}


def test_exact_retry_with_original_token_is_idempotent(adapter):
    assert adapter.compare_and_set(adapter.read(), _snapshot()).applied
    expected = adapter.read()
    first = adapter.compare_and_set(expected, _snapshot(1))
    retry = adapter.compare_and_set(expected, _snapshot(1))
    assert first.code is StrictSnapshotWriteCode.APPLIED
    assert retry.code is StrictSnapshotWriteCode.ALREADY_APPLIED
    assert retry.positions == first.positions


def test_ambiguous_commit_recovers_as_already_applied(redis_client):
    redis_client.flushdb()
    normal = StrictRedisPositionSnapshotAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )
    expected = normal.read()

    def commit_then_timeout(*args):
        redis_client.eval(*args)
        raise TimeoutError("lost acknowledgement")

    uncertain = StrictRedisPositionSnapshotAdapter(
        redis_get=redis_client.get, redis_eval=commit_then_timeout
    )
    assert uncertain.compare_and_set(expected, _snapshot()).code is (
        StrictSnapshotWriteCode.UNKNOWN
    )
    assert normal.compare_and_set(expected, _snapshot()).code is (
        StrictSnapshotWriteCode.ALREADY_APPLIED
    )


@pytest.mark.parametrize(
    "proposed",
    [
        [],
        {"btcusdt": {"qty": 1}},
        {" BTCUSDT": {"qty": 1}},
        {"BTCUSDT": "bad"},
        {"BTCUSDT": {"qty": float("nan")}},
    ],
)
def test_invalid_snapshot_never_reaches_redis(adapter, proposed):
    result = adapter.compare_and_set(adapter.read(), proposed)
    assert result.code is StrictSnapshotWriteCode.INVALID


def test_malformed_read_cannot_be_used_for_write(adapter, redis_client):
    redis_client.set(PM_POSITIONS_KEY, "not-json")
    read = adapter.read()
    assert read.code is StrictSnapshotReadCode.MALFORMED
    result = adapter.compare_and_set(read, _snapshot())
    assert result.code is StrictSnapshotWriteCode.MALFORMED
    assert redis_client.get(PM_POSITIONS_KEY) == b"not-json"


def test_forged_read_token_is_invalid(adapter):
    forged = StrictSnapshotRead(
        StrictSnapshotReadCode.FOUND,
        positions=_snapshot(9),
        raw_token='{"BTCUSDT":{"qty":2}}',
    )
    assert adapter.compare_and_set(forged, _snapshot(1)).code is (
        StrictSnapshotWriteCode.INVALID
    )


def test_backend_unavailable_is_typed():
    adapter = StrictRedisPositionSnapshotAdapter(
        redis_get=lambda _key: (_ for _ in ()).throw(ConnectionError("down")),
        redis_eval=lambda *_args: None,
    )
    read = adapter.read()
    assert read.code is StrictSnapshotReadCode.UNAVAILABLE
    assert adapter.compare_and_set(read, _snapshot()).code is (
        StrictSnapshotWriteCode.UNAVAILABLE
    )
