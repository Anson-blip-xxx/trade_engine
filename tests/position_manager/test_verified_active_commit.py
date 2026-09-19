"""Atomic verified-protection ACTIVE commit against disposable Redis."""

import shutil
import subprocess
import time
from dataclasses import replace
from uuid import uuid4

import pytest
import redis

from position_identity.authority import (
    AuthorityProvenance,
    AuthorityStatus,
    SlotAuthority,
)
from position_identity.projection import LivePositionProjection
from position_identity.slot import ExchangePositionKey
from position_protection.desired import DesiredProtectionRecord, ProtectionStatus
from position_protection.verification import (
    ExchangeExposureObservation,
    ExchangeProtectionObservation,
    ProtectionVerificationCode,
)
from position_protection.verification_commit import (
    RedisVerifiedActiveCommitAdapter,
    VerifiedActiveCommitCode,
)


@pytest.fixture(scope="module")
def redis_client(tmp_path_factory):
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is not installed")
    directory = tmp_path_factory.mktemp("verified-active-redis")
    socket = directory / "redis.sock"
    process = subprocess.Popen(
        [executable, "--port", "0", "--unixsocket", str(socket), "--save", "", "--appendonly", "no", "--dir", str(directory)],
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


def _state(client):
    client.flushdb()
    key = ExchangePositionKey.one_way(account_principal_id="verify-commit", environment="SANDBOX", symbol="BTCUSDT")
    episode = str(uuid4())
    authority = SlotAuthority.initial_flat(key, now=1).allocate_episode(episode_id=episode, provenance=AuthorityProvenance.MIGRATED, status=AuthorityStatus.ACTIVE, legacy_position_id_alias="legacy:1", now=2)
    projection = LivePositionProjection(exchange_position_key=key, episode_id=episode, slot_generation=1, identity_provenance="MIGRATED", state_revision=1, last_operation_id="project", side="LONG", system="S6", quantity=2, entry_price=100, opened_at=1, updated_at=2, legacy_position_id_alias="legacy:1")
    desired = DesiredProtectionRecord.initial_pending(exchange_position_key=key, episode_id=episode, slot_generation=1, desired_intent_id="protect", trigger_price=90, covered_quantity=2, closing_side="SELL", operation_id="declare", now=2).transition(status="SUBMITTING", operation_id="submit", now=3).transition(status="SUBMITTING", operation_id="alias", now=4, exchange_algo_aliases=("77",), allow_same_status_alias_repair=True)
    digest = key.canonical_digest()
    client.set(key.to_storage_key(), authority.to_json())
    client.set(f"pm:position-projection:v1:{digest}", projection.to_json())
    client.set(f"pm:desired-protection:v1:{digest}", desired.to_json())
    exposure = ExchangeExposureObservation(key, "LONG", 2, 5)
    order = ExchangeProtectionObservation(key, "77", "WORKING", "STOP_MARKET", True, "SELL", 90, 2, 5)
    return key, authority, projection, desired, exposure, order


def _commit(adapter, state, **overrides):
    key, _authority, _projection, _desired, exposure, order = state
    values = {
        "exchange_position_key": key,
        "exposure": exposure,
        "orders": (order,),
        "operation_id": "verify:1",
        "now": 6,
        "max_evidence_age": 5,
        "quantity_tolerance": 0,
        "trigger_tolerance": 0,
    }
    values.update(overrides)
    return adapter.commit(**values)


def test_verified_evidence_atomically_transitions_desired_active(redis_client):
    state = _state(redis_client)
    adapter = RedisVerifiedActiveCommitAdapter(redis_get=redis_client.get, redis_eval=redis_client.eval)
    result = _commit(adapter, state)
    assert result.code is VerifiedActiveCommitCode.APPLIED
    assert result.desired.status is ProtectionStatus.ACTIVE
    assert result.desired.last_operation_id == "verify:1"
    assert result.verification.verified


def test_unverified_evidence_performs_no_write(redis_client):
    state = _state(redis_client)
    adapter = RedisVerifiedActiveCommitAdapter(redis_get=redis_client.get, redis_eval=redis_client.eval)
    bad = replace(state[-1], reduce_only=False)
    result = _commit(adapter, state, orders=(bad,))
    assert result.code is VerifiedActiveCommitCode.NOT_VERIFIED
    assert result.verification.code is ProtectionVerificationCode.ORDER_SPEC_MISMATCH
    digest = state[0].canonical_digest()
    stored = DesiredProtectionRecord.from_json(redis_client.get(f"pm:desired-protection:v1:{digest}"))
    assert stored.status is ProtectionStatus.SUBMITTING


@pytest.mark.parametrize("target", ["authority", "projection", "desired"])
def test_canonical_change_after_verification_is_stale(redis_client, target):
    state = _state(redis_client)
    key, authority, projection, desired, _exposure, _order = state
    digest = key.canonical_digest()
    mapping = {"authority": (key.to_storage_key(), authority.to_json() + " "), "projection": (f"pm:position-projection:v1:{digest}", projection.to_json() + " "), "desired": (f"pm:desired-protection:v1:{digest}", desired.to_json() + " ")}

    def mutate_then_eval(*args):
        redis_client.set(*mapping[target])
        return redis_client.eval(*args)

    adapter = RedisVerifiedActiveCommitAdapter(redis_get=redis_client.get, redis_eval=mutate_then_eval)
    assert _commit(adapter, state).code is VerifiedActiveCommitCode.STALE


def test_ambiguous_commit_recovers_by_same_operation_without_new_evidence(redis_client):
    state = _state(redis_client)

    def commit_then_timeout(*args):
        redis_client.eval(*args)
        raise TimeoutError("lost acknowledgement")

    uncertain = RedisVerifiedActiveCommitAdapter(redis_get=redis_client.get, redis_eval=commit_then_timeout)
    assert _commit(uncertain, state).code is VerifiedActiveCommitCode.UNKNOWN
    normal = RedisVerifiedActiveCommitAdapter(redis_get=redis_client.get, redis_eval=redis_client.eval)
    retry = _commit(normal, state, now=100, max_evidence_age=0)
    assert retry.code is VerifiedActiveCommitCode.ALREADY_ACTIVE
    assert retry.desired.last_operation_id == "verify:1"


def test_active_from_other_operation_requires_fresh_verification(redis_client):
    state = _state(redis_client)
    adapter = RedisVerifiedActiveCommitAdapter(redis_get=redis_client.get, redis_eval=redis_client.eval)
    assert _commit(adapter, state).applied
    stale = _commit(adapter, state, operation_id="verify:2", now=100, max_evidence_age=1)
    assert stale.code is VerifiedActiveCommitCode.NOT_VERIFIED
    assert stale.verification.code is ProtectionVerificationCode.STALE_EVIDENCE


def test_missing_and_backend_failure_are_typed(redis_client):
    state = _state(redis_client)
    redis_client.delete(state[0].to_storage_key())
    adapter = RedisVerifiedActiveCommitAdapter(redis_get=redis_client.get, redis_eval=redis_client.eval)
    assert _commit(adapter, state).code is VerifiedActiveCommitCode.NOT_FOUND
    down = RedisVerifiedActiveCommitAdapter(redis_get=lambda _key: (_ for _ in ()).throw(ConnectionError("down")), redis_eval=lambda *_args: None)
    assert _commit(down, state).code is VerifiedActiveCommitCode.UNAVAILABLE
