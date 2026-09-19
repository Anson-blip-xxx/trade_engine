"""R4B controlled legacy migration handoff against isolated Redis."""

import shutil
import subprocess
import time
from uuid import uuid4

import pytest
import redis

from position_identity.adoption import QuarantineReason
from position_identity.authority import AuthorityProvenance, AuthorityStatus
from position_identity.authority_redis import RedisSlotAuthorityAdapter
from position_identity.migration_handoff import (
    LegacyMigrationHandoffCode,
    RedisLegacyMigrationHandoffAdapter,
)
from position_identity.slot import ExchangePositionKey


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id="r4b-migration",
        environment="SANDBOX",
        symbol="BTCUSDT",
    )


def _row(**changes):
    row = {
        "symbol": "BTCUSDT",
        "position_id": "legacy:btc:1",
        "side": "LONG",
        "system": "S6",
        "qty": 2,
        "entry": 100,
        "open_time": 1,
    }
    row.update(changes)
    return row


@pytest.fixture(scope="module")
def redis_client(tmp_path_factory):
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is not installed")
    directory = tmp_path_factory.mktemp("r4b-migration-redis")
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
    return RedisLegacyMigrationHandoffAdapter(
        redis_get=redis_client.get,
        redis_eval=redis_client.eval,
    )


def _migrate(adapter, **changes):
    values = {
        "exchange_position_key": _slot(),
        "candidate_episode_id": str(uuid4()),
        "legacy_row": _row(),
        "exchange_side": "LONG",
        "exchange_quantity": 2,
        "quantity_tolerance": 0,
        "mixed_version": False,
        "allow_authority_initialization": True,
        "operation_id": "legacy-migration:btc:1",
        "now": 2,
    }
    values.update(changes)
    return adapter.migrate(**values)


def test_fresh_migration_atomically_writes_authority_and_projection(
    adapter, redis_client
):
    result = _migrate(adapter)
    assert result.code is LegacyMigrationHandoffCode.APPLIED
    assert result.authority.status is AuthorityStatus.ACTIVE
    assert result.authority.provenance is AuthorityProvenance.MIGRATED
    assert result.authority.legacy_position_id_alias == "legacy:btc:1"
    assert result.authority.slot_generation == 1
    assert result.projection.episode_id == result.authority.episode_id
    assert result.projection.identity_provenance is AuthorityProvenance.MIGRATED
    assert len(redis_client.keys("*")) == 2


def test_retry_uses_adopted_episode_not_new_candidate(adapter):
    first = _migrate(adapter)
    retry = _migrate(adapter, candidate_episode_id=str(uuid4()), now=3)
    assert first.applied
    assert retry.code is LegacyMigrationHandoffCode.ALREADY_APPLIED
    assert retry.authority.episode_id == first.authority.episode_id
    assert retry.projection == first.projection


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"exchange_side": "SHORT"}, QuarantineReason.EXCHANGE_LOCAL_MISMATCH),
        ({"exchange_quantity": 3}, QuarantineReason.EXCHANGE_LOCAL_MISMATCH),
        ({"mixed_version": True}, QuarantineReason.MIXED_VERSION),
        (
            {"legacy_row": _row(position_id="")},
            QuarantineReason.MISSING_LEGACY_ID,
        ),
    ],
)
def test_unsafe_legacy_evidence_is_quarantined_without_writes(
    adapter, redis_client, changes, reason
):
    result = _migrate(adapter, **changes)
    assert result.code is LegacyMigrationHandoffCode.QUARANTINED
    assert result.reason is reason
    assert redis_client.keys("*") == []


def test_missing_authority_requires_explicit_initialization(adapter, redis_client):
    result = _migrate(adapter, allow_authority_initialization=False)
    assert result.code is LegacyMigrationHandoffCode.QUARANTINED
    assert result.reason is QuarantineReason.AUTHORITY_MISSING
    assert redis_client.keys("*") == []


def test_native_owner_is_never_converted_to_migrated(adapter, redis_client):
    authority = RedisSlotAuthorityAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )
    flat = authority.initialize_flat(_slot(), now=1).authority
    native = authority.allocate_new_episode(
        _slot(),
        candidate_episode_id=str(uuid4()),
        expected_revision=flat.revision,
        expected_slot_generation=flat.slot_generation,
        expected_episode_id=flat.episode_id,
        now=2,
    ).authority

    result = _migrate(adapter, now=3)

    assert result.code is LegacyMigrationHandoffCode.EXISTING_OWNER
    assert result.authority == native
    assert result.authority.provenance is AuthorityProvenance.NATIVE
    assert len(redis_client.keys("*")) == 1


def test_pre_read_failure_is_unavailable():
    adapter = RedisLegacyMigrationHandoffAdapter(
        redis_get=lambda _key: (_ for _ in ()).throw(ConnectionError("down")),
        redis_eval=lambda *_args: None,
    )
    assert _migrate(adapter).code is LegacyMigrationHandoffCode.UNAVAILABLE


def test_ambiguous_ack_is_unknown_but_retry_recovers(redis_client):
    redis_client.flushdb()

    def commit_then_timeout(*args):
        redis_client.eval(*args)
        raise TimeoutError("lost acknowledgement")

    uncertain = RedisLegacyMigrationHandoffAdapter(
        redis_get=redis_client.get,
        redis_eval=commit_then_timeout,
    )
    assert _migrate(uncertain).code is LegacyMigrationHandoffCode.UNKNOWN

    retry = RedisLegacyMigrationHandoffAdapter(
        redis_get=redis_client.get,
        redis_eval=redis_client.eval,
    )
    assert _migrate(retry, now=3).code is LegacyMigrationHandoffCode.ALREADY_APPLIED
