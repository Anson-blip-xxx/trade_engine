"""R4 native-open three-record handoff against disposable Redis."""

import shutil
import subprocess
import time
from uuid import uuid4

import pytest
import redis

from position_identity.authority import AuthorityStatus
from position_identity.authority_redis import RedisSlotAuthorityAdapter
from position_identity.slot import ExchangePositionKey
from position_protection.handoff import NativeOpenHandoffCode
from position_protection.handoff_redis import RedisNativeOpenHandoffAdapter


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id="r4-native",
        environment="SANDBOX",
        symbol="BTCUSDT",
    )


@pytest.fixture(scope="module")
def redis_client(tmp_path_factory):
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is not installed")
    directory = tmp_path_factory.mktemp("r4-redis")
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
    return RedisNativeOpenHandoffAdapter(
        redis_get=redis_client.get,
        redis_eval=redis_client.eval,
    )


def _open(adapter, *, episode=None, order="42", now=2):
    return adapter.open_native(
        exchange_position_key=_slot(),
        candidate_episode_id=episode or str(uuid4()),
        desired_intent_id=f"stop:{order}",
        trigger_price=90,
        covered_quantity=2,
        closing_side="SELL",
        position_side="LONG",
        system="S6",
        entry_price=100,
        opened_at=1,
        operation_id=f"native-open:{order}",
        now=now,
    )


def test_fresh_open_atomically_establishes_three_records(adapter, redis_client):
    episode = str(uuid4())
    result = _open(adapter, episode=episode)
    assert result.code is NativeOpenHandoffCode.APPLIED
    assert result.authority.status is AuthorityStatus.ACTIVE
    assert result.authority.episode_id == episode
    assert result.authority.slot_generation == 1
    assert result.projection.state_revision == 1
    assert result.desired.protection_generation == 1
    assert result.desired.revision == 2
    assert result.desired.status.value == "SUBMITTING"
    digest = _slot().canonical_digest()
    assert redis_client.mget(
        _slot().to_storage_key(),
        f"pm:position-projection:v1:{digest}",
        f"pm:desired-protection:v1:{digest}",
    ) == [
        result.authority.to_json().encode(),
        result.projection.to_json().encode(),
        result.desired.to_json().encode(),
    ]


def test_same_open_order_retry_is_idempotent_across_candidate_uuid(adapter):
    first = _open(adapter)
    assert first.applied
    retry = _open(adapter)
    assert retry.code is NativeOpenHandoffCode.ALREADY_APPLIED


def test_other_active_episode_conflicts_without_mutation(adapter, redis_client):
    first = _open(adapter)
    before = redis_client.mget(*redis_client.keys("*"))
    conflict = _open(adapter, order="43")
    assert conflict.code is NativeOpenHandoffCode.CONFLICT
    assert redis_client.mget(*redis_client.keys("*")) == before
    assert first.authority.episode_id != conflict.authority


def test_flat_reopen_replaces_stale_projection_and_desired(adapter, redis_client):
    first = _open(adapter)
    authority_store = RedisSlotAuthorityAdapter(
        redis_get=redis_client.get,
        redis_eval=redis_client.eval,
    )
    flat = authority_store.transition_active_to_flat(
        _slot(),
        expected_episode_id=first.authority.episode_id,
        expected_slot_generation=first.authority.slot_generation,
        expected_revision=first.authority.revision,
        now=3,
    )
    assert flat.applied
    second = _open(adapter, order="43", now=4)
    assert second.code is NativeOpenHandoffCode.APPLIED
    assert second.authority.slot_generation == 2
    assert second.authority.episode_id != first.authority.episode_id
    assert second.projection.episode_id == second.authority.episode_id
    assert second.desired.episode_id == second.authority.episode_id


def test_orphan_state_and_backend_errors_fail_closed(adapter, redis_client):
    digest = _slot().canonical_digest()
    redis_client.set(f"pm:position-projection:v1:{digest}", "orphan")
    assert _open(adapter).code is NativeOpenHandoffCode.CONFLICT

    unavailable = RedisNativeOpenHandoffAdapter(
        redis_get=lambda _key: (_ for _ in ()).throw(ConnectionError("down")),
        redis_eval=redis_client.eval,
    )
    assert _open(unavailable).code is NativeOpenHandoffCode.UNAVAILABLE

    unknown = RedisNativeOpenHandoffAdapter(
        redis_get=lambda _key: None,
        redis_eval=lambda *_args: (_ for _ in ()).throw(TimeoutError("lost")),
    )
    assert _open(unknown).code is NativeOpenHandoffCode.UNKNOWN
