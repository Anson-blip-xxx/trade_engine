"""Authority-fenced projection reduction against isolated Redis."""

import shutil
import subprocess
import time
from uuid import uuid4

import pytest
import redis

from position_identity.authority import AuthorityStatus
from position_identity.authority_redis import RedisSlotAuthorityAdapter
from position_identity.migration_handoff import RedisLegacyMigrationHandoffAdapter
from position_identity.projection_mutation import (
    ProjectionMutationCode,
    RedisProjectionMutationAdapter,
)
from position_identity.slot import ExchangePositionKey


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id="projection-mutation",
        environment="SANDBOX",
        symbol="BTCUSDT",
    )


@pytest.fixture(scope="module")
def redis_client(tmp_path_factory):
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is not installed")
    directory = tmp_path_factory.mktemp("projection-mutation-redis")
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
def state(redis_client):
    redis_client.flushdb()
    migrated = RedisLegacyMigrationHandoffAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    ).migrate(
        exchange_position_key=_slot(),
        candidate_episode_id=str(uuid4()),
        legacy_row={
            "symbol": "BTCUSDT",
            "position_id": "legacy:btc:1",
            "side": "LONG",
            "system": "S6",
            "qty": 10,
            "entry": 100,
            "open_time": 1,
        },
        exchange_side="LONG",
        exchange_quantity=10,
        quantity_tolerance=0,
        mixed_version=False,
        allow_authority_initialization=True,
        operation_id="migration:btc:1",
        now=2,
    )
    adapter = RedisProjectionMutationAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )
    return adapter, migrated


def _reduce(adapter, migrated, **changes):
    values = {
        "expected_episode_id": migrated.authority.episode_id,
        "expected_slot_generation": migrated.authority.slot_generation,
        "expected_authority_revision": migrated.authority.revision,
        "expected_projection_revision": migrated.projection.state_revision,
        "new_quantity": 6,
        "operation_id": "partial-close:42",
        "now": 3,
    }
    values.update(changes)
    return adapter.reduce_quantity(_slot(), **values)


def test_reduce_is_authority_and_projection_fenced(state):
    adapter, migrated = state
    result = _reduce(adapter, migrated)
    assert result.code is ProjectionMutationCode.APPLIED
    assert result.projection.quantity == 6
    assert result.projection.state_revision == migrated.projection.state_revision + 1
    assert result.projection.episode_id == migrated.authority.episode_id


def test_exact_retry_is_idempotent_with_original_expectation(state):
    adapter, migrated = state
    first = _reduce(adapter, migrated)
    retry = _reduce(adapter, migrated, now=4)
    assert first.applied
    assert retry.code is ProjectionMutationCode.ALREADY_APPLIED
    assert retry.projection == first.projection


def test_different_operation_with_old_revision_is_stale(state):
    adapter, migrated = state
    assert _reduce(adapter, migrated).applied
    stale = _reduce(adapter, migrated, operation_id="partial-close:43", new_quantity=5)
    assert stale.code is ProjectionMutationCode.STALE
    assert stale.projection.quantity == 6


def test_flat_authority_blocks_even_exact_projection_retry(state, redis_client):
    adapter, migrated = state
    assert _reduce(adapter, migrated).applied
    authority = RedisSlotAuthorityAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )
    flat = authority.transition_active_to_flat(
        _slot(),
        expected_episode_id=migrated.authority.episode_id,
        expected_slot_generation=migrated.authority.slot_generation,
        expected_revision=migrated.authority.revision,
        now=4,
    )
    assert flat.authority.status is AuthorityStatus.FLAT
    assert _reduce(adapter, migrated, now=5).code is ProjectionMutationCode.STALE


@pytest.mark.parametrize("quantity", [10, 11, 0, -1, float("nan")])
def test_non_reduction_or_invalid_quantity_never_writes(state, quantity):
    adapter, migrated = state
    result = _reduce(adapter, migrated, new_quantity=quantity)
    assert result.code is ProjectionMutationCode.INVALID
    assert migrated.projection.state_revision == 1


def test_authority_change_between_read_and_lua_is_stale(state, redis_client):
    _adapter, migrated = state
    authority = RedisSlotAuthorityAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )

    def flatten_then_eval(*args):
        flat = authority.transition_active_to_flat(
            _slot(),
            expected_episode_id=migrated.authority.episode_id,
            expected_slot_generation=migrated.authority.slot_generation,
            expected_revision=migrated.authority.revision,
            now=3,
        )
        assert flat.applied
        return redis_client.eval(*args)

    adapter = RedisProjectionMutationAdapter(
        redis_get=redis_client.get, redis_eval=flatten_then_eval
    )
    result = _reduce(adapter, migrated)
    assert result.code is ProjectionMutationCode.STALE
    assert result.projection.quantity == 10


def test_ambiguous_commit_recovers_as_already_applied(state, redis_client):
    _adapter, migrated = state

    def commit_then_timeout(*args):
        redis_client.eval(*args)
        raise TimeoutError("lost acknowledgement")

    uncertain = RedisProjectionMutationAdapter(
        redis_get=redis_client.get, redis_eval=commit_then_timeout
    )
    assert _reduce(uncertain, migrated).code is ProjectionMutationCode.UNKNOWN
    retry = RedisProjectionMutationAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )
    assert _reduce(retry, migrated, now=4).code is (
        ProjectionMutationCode.ALREADY_APPLIED
    )


def test_missing_and_backend_failure_are_typed(redis_client):
    redis_client.flushdb()
    missing = RedisProjectionMutationAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )
    fake_episode = str(uuid4())
    result = missing.reduce_quantity(
        _slot(),
        expected_episode_id=fake_episode,
        expected_slot_generation=1,
        expected_authority_revision=2,
        expected_projection_revision=1,
        new_quantity=1,
        operation_id="partial-close:missing",
        now=1,
    )
    assert result.code is ProjectionMutationCode.NOT_FOUND

    down = RedisProjectionMutationAdapter(
        redis_get=lambda _key: (_ for _ in ()).throw(ConnectionError("down")),
        redis_eval=redis_client.eval,
    )
    result = down.reduce_quantity(
        _slot(),
        expected_episode_id=fake_episode,
        expected_slot_generation=1,
        expected_authority_revision=2,
        expected_projection_revision=1,
        new_quantity=1,
        operation_id="partial-close:down",
        now=1,
    )
    assert result.code is ProjectionMutationCode.UNAVAILABLE
