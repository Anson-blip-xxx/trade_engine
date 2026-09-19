"""R3 conditional ACK writeback against isolated disposable Redis."""

import json
import shutil
import subprocess
import time

import pytest
import redis

from position_identity.authority import SlotAuthority
from position_identity.projection import LivePositionProjection
from position_identity.slot import ExchangePositionKey
from position_protection.claim import ProtectionMutationClaim
from position_protection.desired import DesiredProtectionRecord
from position_protection.writeback import WritebackCode
from position_protection.writeback_redis import (
    RedisConditionalAliasWritebackAdapter,
)

EPISODE = "6a4717d8-7390-4386-9878-56ff2981f93d"


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id="r3-test", environment="SANDBOX", symbol="BTCUSDT"
    )


@pytest.fixture(scope="module")
def redis_client(tmp_path_factory):
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is not installed")
    directory = tmp_path_factory.mktemp("r3-redis")
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
    slot = _slot()
    authority = SlotAuthority(
        exchange_position_key=slot,
        episode_id=EPISODE,
        slot_generation=3,
        status="ACTIVE",
        provenance="NATIVE",
        revision=4,
        legacy_position_id_alias=None,
        created_at=1,
        updated_at=2,
    )
    desired = DesiredProtectionRecord.initial_pending(
        exchange_position_key=slot,
        episode_id=EPISODE,
        slot_generation=3,
        desired_intent_id="stop-1",
        trigger_price=90,
        covered_quantity=2,
        closing_side="SELL",
        operation_id="declare",
        now=2,
    ).transition(status="SUBMITTING", operation_id="claim", now=3)
    projection = LivePositionProjection(
        exchange_position_key=slot,
        episode_id=EPISODE,
        slot_generation=3,
        identity_provenance="NATIVE",
        state_revision=5,
        last_operation_id="position-open",
        side="LONG",
        system="S6",
        quantity=2,
        entry_price=100,
        opened_at=1,
        updated_at=2,
    )
    claim = ProtectionMutationClaim(
        exchange_position_key=slot,
        episode_id=EPISODE,
        slot_generation=3,
        protection_generation=1,
        authority_revision=4,
        owner_token="owner-1",
        fencing_token=7,
    )
    digest = slot.canonical_digest()
    redis_client.set(slot.to_storage_key(), authority.to_json())
    redis_client.set(f"pm:desired-protection:v1:{digest}", desired.to_json())
    redis_client.set(f"pm:position-projection:v1:{digest}", projection.to_json())
    redis_client.set(
        f"pm:protection-claim:v1:{digest}",
        json.dumps(
            {
                "schema_version": 1,
                "owner_token": "owner-1",
                "fencing_token": 7,
                "authority_revision": 4,
                "episode_id": EPISODE,
                "slot_generation": 3,
                "protection_generation": 1,
            }
        ),
    )
    adapter = RedisConditionalAliasWritebackAdapter(redis_eval=redis_client.eval)
    return adapter, claim, desired, projection, redis_client


def _write(adapter, claim, desired, projection, **overrides):
    values = {
        "claim": claim,
        "expected_desired_revision": desired.revision,
        "expected_projection_revision": projection.state_revision,
        "exchange_algo_alias": "algo-77",
        "operation_id": "ack-77",
        "now": 4,
    }
    values.update(overrides)
    return adapter.writeback(**values)


def test_writeback_appends_alias_without_claiming_active(state):
    adapter, claim, desired, projection, _redis = state
    result = _write(adapter, claim, desired, projection)
    assert result.code is WritebackCode.APPLIED
    assert result.desired.exchange_algo_aliases == ("algo-77",)
    assert result.desired.status.value == "SUBMITTING"
    assert result.desired.revision == desired.revision + 1


def test_same_operation_retry_is_idempotent(state):
    adapter, claim, desired, projection, _redis = state
    assert _write(adapter, claim, desired, projection).applied
    retry = _write(adapter, claim, desired, projection)
    assert retry.code is WritebackCode.ALREADY_APPLIED
    assert retry.desired.exchange_algo_aliases == ("algo-77",)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_desired_revision", 1),
        ("expected_projection_revision", 4),
    ],
)
def test_stale_revision_cannot_write(state, field, value):
    adapter, claim, desired, projection, _redis = state
    result = _write(adapter, claim, desired, projection, **{field: value})
    assert result.code is WritebackCode.STALE


def test_lost_claim_cannot_write(state):
    adapter, claim, desired, projection, client = state
    client.delete(f"pm:protection-claim:v1:{_slot().canonical_digest()}")
    assert _write(adapter, claim, desired, projection).code is WritebackCode.CLAIM_LOST


def test_claim_from_old_authority_revision_cannot_write(state):
    adapter, claim, desired, projection, client = state
    key = f"pm:protection-claim:v1:{_slot().canonical_digest()}"
    raw = json.loads(client.get(key))
    raw["authority_revision"] = 3
    client.set(key, json.dumps(raw))
    assert _write(adapter, claim, desired, projection).code is WritebackCode.CLAIM_LOST


def test_malformed_desired_fails_closed(state):
    adapter, claim, desired, projection, client = state
    key = f"pm:desired-protection:v1:{_slot().canonical_digest()}"
    raw = json.loads(client.get(key))
    raw.pop("updated_at")
    client.set(key, json.dumps(raw))
    assert _write(adapter, claim, desired, projection).code is WritebackCode.MALFORMED


def test_decreasing_writeback_timestamp_is_invalid(state):
    adapter, claim, desired, projection, _client = state
    assert (
        _write(adapter, claim, desired, projection, now=2).code is WritebackCode.INVALID
    )


def test_eval_exception_is_unknown_not_success(state):
    _adapter, claim, desired, projection, _redis = state

    def fail(*_args):
        raise RuntimeError("ack lost")

    adapter = RedisConditionalAliasWritebackAdapter(redis_eval=fail)
    result = _write(adapter, claim, desired, projection)
    assert result.code is WritebackCode.UNKNOWN
    assert not result.applied
