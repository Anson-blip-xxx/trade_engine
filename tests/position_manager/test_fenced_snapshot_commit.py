"""Atomic canonical-token + legacy-snapshot commit against isolated Redis."""

import json
import shutil
import subprocess
import time
from uuid import uuid4

import pytest
import redis

from position_identity.authority_redis import RedisSlotAuthorityAdapter
from position_identity.migration_handoff import RedisLegacyMigrationHandoffAdapter
from position_identity.slot import ExchangePositionKey
from position_state.fenced_snapshot import (
    FencedSnapshotCommitCode,
    RedisFencedSnapshotCommitAdapter,
)
from position_state.strict_snapshot import PM_POSITIONS_KEY


def _slot(symbol):
    return ExchangePositionKey.one_way(
        account_principal_id="fenced-snapshot",
        environment="SANDBOX",
        symbol=symbol,
    )


def _row(qty=10):
    return {"entry": 100, "qty": qty, "side": "LONG", "system": "S6"}


@pytest.fixture(scope="module")
def redis_client(tmp_path_factory):
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is not installed")
    directory = tmp_path_factory.mktemp("fenced-snapshot-redis")
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


@pytest.fixture(autouse=True)
def reset_redis(redis_client):
    redis_client.flushdb()


@pytest.fixture
def adapter(redis_client):
    return RedisFencedSnapshotCommitAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )


def _store_snapshot(redis_client, snapshot):
    redis_client.set(
        PM_POSITIONS_KEY,
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
    )


def _migrate(redis_client, symbol="BTCUSDT", qty=10):
    return RedisLegacyMigrationHandoffAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    ).migrate(
        exchange_position_key=_slot(symbol),
        candidate_episode_id=str(uuid4()),
        legacy_row={
            "symbol": symbol,
            "position_id": f"legacy:{symbol}:1",
            "side": "LONG",
            "system": "S6",
            "qty": qty,
            "entry": 100,
            "open_time": 1,
        },
        exchange_side="LONG",
        exchange_quantity=qty,
        quantity_tolerance=0,
        mixed_version=False,
        allow_authority_initialization=True,
        operation_id=f"migration:{symbol}:1",
        now=2,
    )


def test_all_legacy_snapshot_commits_normally(adapter):
    result = adapter.commit(
        proposed_snapshot={"BTCUSDT": _row()},
        slots={"BTCUSDT": _slot("BTCUSDT")},
    )
    assert result.code is FencedSnapshotCommitCode.APPLIED
    assert result.positions == {"BTCUSDT": _row()}


def test_active_canonical_mutation_is_filtered_but_other_update_commits(
    adapter, redis_client
):
    current = {"BTCUSDT": _row(), "ETHUSDT": _row(3)}
    _store_snapshot(redis_client, current)
    _migrate(redis_client)
    result = adapter.commit(
        proposed_snapshot={"BTCUSDT": _row(6), "ETHUSDT": _row(4)},
        slots={
            "BTCUSDT": _slot("BTCUSDT"),
            "ETHUSDT": _slot("ETHUSDT"),
        },
    )
    assert result.code is FencedSnapshotCommitCode.FILTERED_APPLIED
    assert result.positions == {"BTCUSDT": _row(), "ETHUSDT": _row(4)}
    assert result.plan.fenced_symbols == ("BTCUSDT",)


def test_flat_canonical_row_is_removed(adapter, redis_client):
    _store_snapshot(redis_client, {"BTCUSDT": _row()})
    migrated = _migrate(redis_client)
    authority = RedisSlotAuthorityAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )
    assert authority.transition_active_to_flat(
        _slot("BTCUSDT"),
        expected_episode_id=migrated.authority.episode_id,
        expected_slot_generation=migrated.authority.slot_generation,
        expected_revision=migrated.authority.revision,
        now=3,
    ).applied
    result = adapter.commit(
        proposed_snapshot={"BTCUSDT": _row()},
        slots={"BTCUSDT": _slot("BTCUSDT")},
    )
    assert result.code is FencedSnapshotCommitCode.FILTERED_APPLIED
    assert result.positions == {}


def test_canonical_change_between_plan_and_lua_is_stale(redis_client):
    _store_snapshot(redis_client, {"BTCUSDT": _row()})
    migrated = _migrate(redis_client)
    authority = RedisSlotAuthorityAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )

    def flatten_then_eval(*args):
        assert authority.transition_active_to_flat(
            _slot("BTCUSDT"),
            expected_episode_id=migrated.authority.episode_id,
            expected_slot_generation=migrated.authority.slot_generation,
            expected_revision=migrated.authority.revision,
            now=3,
        ).applied
        return redis_client.eval(*args)

    adapter = RedisFencedSnapshotCommitAdapter(
        redis_get=redis_client.get, redis_eval=flatten_then_eval
    )
    result = adapter.commit(
        proposed_snapshot={"BTCUSDT": _row(6)},
        slots={"BTCUSDT": _slot("BTCUSDT")},
    )
    assert result.code is FencedSnapshotCommitCode.STALE
    assert result.positions == {"BTCUSDT": _row()}


def test_snapshot_change_between_plan_and_lua_is_stale(redis_client):
    _store_snapshot(redis_client, {"BTCUSDT": _row()})

    def replace_then_eval(*args):
        _store_snapshot(redis_client, {"ETHUSDT": _row(3)})
        return redis_client.eval(*args)

    adapter = RedisFencedSnapshotCommitAdapter(
        redis_get=redis_client.get, redis_eval=replace_then_eval
    )
    result = adapter.commit(
        proposed_snapshot={"BTCUSDT": _row(6)},
        slots={"BTCUSDT": _slot("BTCUSDT")},
    )
    assert result.code is FencedSnapshotCommitCode.STALE
    assert result.positions == {"ETHUSDT": _row(3)}


def test_filtered_exact_retry_is_typed(adapter, redis_client):
    _store_snapshot(redis_client, {"BTCUSDT": _row(), "ETHUSDT": _row(3)})
    _migrate(redis_client)
    proposed = {"BTCUSDT": _row(6), "ETHUSDT": _row(4)}
    slots = {
        "BTCUSDT": _slot("BTCUSDT"),
        "ETHUSDT": _slot("ETHUSDT"),
    }
    assert adapter.commit(
        proposed_snapshot=proposed, slots=slots
    ).code is FencedSnapshotCommitCode.FILTERED_APPLIED
    retry = adapter.commit(proposed_snapshot=proposed, slots=slots)
    assert retry.code is FencedSnapshotCommitCode.FILTERED_ALREADY_APPLIED


def test_ambiguous_commit_recovers_as_already_applied(redis_client):
    redis_client.flushdb()

    def commit_then_timeout(*args):
        redis_client.eval(*args)
        raise TimeoutError("lost acknowledgement")

    uncertain = RedisFencedSnapshotCommitAdapter(
        redis_get=redis_client.get, redis_eval=commit_then_timeout
    )
    proposed = {"BTCUSDT": _row()}
    slots = {"BTCUSDT": _slot("BTCUSDT")}
    assert uncertain.commit(
        proposed_snapshot=proposed, slots=slots
    ).code is FencedSnapshotCommitCode.UNKNOWN
    normal = RedisFencedSnapshotCommitAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )
    assert normal.commit(
        proposed_snapshot=proposed, slots=slots
    ).code is FencedSnapshotCommitCode.ALREADY_APPLIED


def test_orphan_canonical_record_rejects_entire_commit(adapter, redis_client):
    projection_key = (
        "pm:position-projection:v1:" + _slot("BTCUSDT").canonical_digest()
    )
    redis_client.set(projection_key, "orphan")
    result = adapter.commit(
        proposed_snapshot={"BTCUSDT": _row()},
        slots={"BTCUSDT": _slot("BTCUSDT")},
    )
    assert result.code is FencedSnapshotCommitCode.FENCE_REJECTED
    assert redis_client.get(PM_POSITIONS_KEY) is None


def test_slot_mapping_must_exactly_cover_batch(adapter):
    result = adapter.commit(
        proposed_snapshot={"BTCUSDT": _row()}, slots={}
    )
    assert result.code is FencedSnapshotCommitCode.INVALID


def test_backend_failure_is_unavailable():
    adapter = RedisFencedSnapshotCommitAdapter(
        redis_get=lambda _key: (_ for _ in ()).throw(ConnectionError("down")),
        redis_eval=lambda *_args: None,
    )
    result = adapter.commit(
        proposed_snapshot={"BTCUSDT": _row()},
        slots={"BTCUSDT": _slot("BTCUSDT")},
    )
    assert result.code is FencedSnapshotCommitCode.UNAVAILABLE
