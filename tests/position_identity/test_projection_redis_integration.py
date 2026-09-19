"""R1 Lua contract tests against an isolated disposable Redis process."""
import json
import shutil
import subprocess
import threading
import time

import pytest
import redis

from position_identity.projection import (
    LivePositionProjection,
    ProjectionReadCode,
    ProjectionWriteCode,
)
from position_identity.projection_redis import RedisLivePositionProjectionAdapter
from position_identity.slot import ExchangePositionKey

EPISODE = '6a4717d8-7390-4386-9878-56ff2981f93d'


def _slot(symbol='BTCUSDT'):
    return ExchangePositionKey.one_way(
        account_principal_id='r1-isolated-test', environment='SANDBOX',
        symbol=symbol)


def _projection(*, slot=None, operation='open-1', revision=1, quantity=2):
    return LivePositionProjection(
        exchange_position_key=slot or _slot(), episode_id=EPISODE,
        slot_generation=3, identity_provenance='NATIVE',
        state_revision=revision, last_operation_id=operation,
        side='LONG', system='S6', quantity=quantity, entry_price=100,
        opened_at=10, updated_at=10 + revision,
    )


@pytest.fixture(scope='module')
def isolated_redis(tmp_path_factory):
    """Start Redis with no TCP listener, persistence, or production config."""
    executable = shutil.which('redis-server')
    if executable is None:
        pytest.skip('redis-server is not installed')
    directory = tmp_path_factory.mktemp('r1-redis')
    socket = directory / 'redis.sock'
    process = subprocess.Popen(
        [
            executable, '--port', '0', '--unixsocket', str(socket),
            '--unixsocketperm', '700', '--save', '', '--appendonly', 'no',
            '--dir', str(directory), '--daemonize', 'no',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    client = redis.Redis(unix_socket_path=str(socket), decode_responses=False)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ''
            pytest.fail(f'isolated redis-server exited: {stderr}')
        try:
            if client.ping():
                break
        except redis.RedisError:
            time.sleep(0.02)
    else:
        process.terminate()
        pytest.fail('isolated redis-server did not become ready')
    yield client
    client.close()
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


@pytest.fixture
def adapter(isolated_redis):
    isolated_redis.flushdb()
    return RedisLivePositionProjectionAdapter(
        redis_get=isolated_redis.get, redis_eval=isolated_redis.eval)


def test_real_lua_create_cas_idempotency_and_conflicts(adapter):
    initial = _projection()
    assert adapter.create(initial).code is ProjectionWriteCode.APPLIED
    assert adapter.create(initial).code is ProjectionWriteCode.ALREADY_APPLIED

    altered_retry = _projection(quantity=9)
    assert adapter.create(altered_retry).code is ProjectionWriteCode.CONFLICT

    revised = initial.revise(operation_id='resize-2', now=20, quantity=3)
    assert adapter.compare_and_set(
        revised, expected_episode_id=EPISODE,
        expected_slot_generation=3,
        expected_revision=1,
    ).code is ProjectionWriteCode.APPLIED
    assert adapter.compare_and_set(
        revised, expected_episode_id=EPISODE,
        expected_slot_generation=3,
        expected_revision=1,
    ).code is ProjectionWriteCode.ALREADY_APPLIED
    assert adapter.get(_slot()).projection == revised


def test_real_lua_allows_exactly_one_concurrent_revision_winner(adapter):
    initial = _projection()
    assert adapter.create(initial).applied
    barrier = threading.Barrier(3)
    outcomes = []

    def update(operation, quantity):
        barrier.wait()
        outcomes.append(adapter.compare_and_set(
            initial.revise(
                operation_id=operation, now=20, quantity=quantity),
            expected_episode_id=EPISODE,
            expected_slot_generation=3,
            expected_revision=1,
        ).code)

    threads = [
        threading.Thread(target=update, args=('resize-a', 3)),
        threading.Thread(target=update, args=('resize-b', 4)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert outcomes.count(ProjectionWriteCode.APPLIED) == 1
    assert outcomes.count(ProjectionWriteCode.STALE) == 1


@pytest.mark.parametrize("raw", [
    "{broken",
    ("{\"schema_version\":1,\"state_revision\":1,\"slot_generation\":3,"
     "\"episode_id\":\"6a4717d8-7390-4386-9878-56ff2981f93d\","
     "\"last_operation_id\":\"polluted\"}"),
    json.dumps({**_projection().to_dict(), "side": "BUY"}),
])
def test_real_lua_never_overwrites_malformed_record(
        adapter, isolated_redis, raw):
    key = adapter._storage_key(_slot())
    isolated_redis.set(key, raw)
    before = isolated_redis.get(key)
    assert adapter.get(_slot()).code is ProjectionReadCode.MALFORMED
    assert adapter.create(_projection()).code is ProjectionWriteCode.MALFORMED
    assert isolated_redis.get(key) == before


def test_invalid_cas_expectations_do_not_call_redis():
    calls = []
    adapter = RedisLivePositionProjectionAdapter(
        redis_get=lambda _key: None,
        redis_eval=lambda *args: calls.append(args),
    )
    projection = _projection(revision=2, operation='resize')
    invalid_values = [
        ('not-a-uuid', 3, 1),
        (EPISODE, 0, 1),
        (EPISODE, 3, 0),
        (EPISODE, True, 1),
    ]
    for episode, generation, revision in invalid_values:
        result = adapter.compare_and_set(
            projection, expected_episode_id=episode,
            expected_slot_generation=generation,
            expected_revision=revision)
        assert result.code is ProjectionWriteCode.INVALID
    assert calls == []
