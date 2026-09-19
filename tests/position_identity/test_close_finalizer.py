"""Canonical close capture/finalize behavior against isolated Redis."""

import shutil
import subprocess
import time
from uuid import uuid4

import pytest
import redis

from position_identity.authority import (
    AuthorityReadCode,
    AuthorityReadResult,
    AuthorityStatus,
)
from position_identity.authority_redis import RedisSlotAuthorityAdapter
from position_identity.close_finalizer import (
    CanonicalCloseFinalizer,
    CloseCaptureCode,
    CloseFinalizeCode,
)
from position_identity.projection_redis import RedisLivePositionProjectionAdapter
from position_identity.slot import ExchangePositionKey
from position_protection.handoff_redis import RedisNativeOpenHandoffAdapter


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id="r4-close",
        environment="SANDBOX",
        symbol="BTCUSDT",
    )


@pytest.fixture(scope="module")
def redis_client(tmp_path_factory):
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is not installed")
    directory = tmp_path_factory.mktemp("r4-close-redis")
    socket = directory / "redis.sock"
    process = subprocess.Popen(
        [executable, "--port", "0", "--unixsocket", str(socket), "--save", "",
         "--appendonly", "no", "--dir", str(directory)],
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
def close_env(redis_client):
    redis_client.flushdb()
    native = RedisNativeOpenHandoffAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )
    opened = native.open_native(
        exchange_position_key=_slot(), candidate_episode_id=str(uuid4()),
        desired_intent_id="stop:42", trigger_price=90, covered_quantity=2,
        closing_side="SELL", position_side="LONG", system="S6",
        entry_price=100, opened_at=1, operation_id="native-open:42", now=2,
    )
    authority = RedisSlotAuthorityAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )
    projection = RedisLivePositionProjectionAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    )
    service = CanonicalCloseFinalizer(
        authority_store=authority, projection_store=projection
    )
    local = {"side": "LONG", "system": "S6", "entry": 100,
             "open_time": 1, "qty": 2}
    return service, authority, opened, local


def test_capture_then_finalize_is_generation_fenced_and_idempotent(close_env):
    service, authority, opened, local = close_env
    captured = service.capture(_slot(), local_position=local)
    assert captured.code is CloseCaptureCode.CAPTURED
    assert captured.fence.episode_id == opened.authority.episode_id
    assert service.finalize(captured.fence, now=3).code is CloseFinalizeCode.APPLIED
    assert service.finalize(captured.fence, now=4).code is CloseFinalizeCode.ALREADY_APPLIED
    current = authority.get_slot_authority(_slot()).authority
    assert current.status is AuthorityStatus.FLAT
    assert current.revision == captured.fence.authority_revision + 1


@pytest.mark.parametrize(
    ("field", "value"),
    [("side", "SHORT"), ("system", "S8"), ("entry", 101),
     ("open_time", 2), ("qty", 3), ("qty", 0)],
)
def test_capture_rejects_local_position_mismatch(close_env, field, value):
    service, _authority, _opened, local = close_env
    local[field] = value
    assert service.capture(_slot(), local_position=local).code is CloseCaptureCode.STALE


def test_old_fence_cannot_release_reopened_generation(close_env, redis_client):
    service, authority, opened, local = close_env
    captured = service.capture(_slot(), local_position=local)
    assert service.finalize(captured.fence, now=3).applied
    reopened = RedisNativeOpenHandoffAdapter(
        redis_get=redis_client.get, redis_eval=redis_client.eval
    ).open_native(
        exchange_position_key=_slot(), candidate_episode_id=str(uuid4()),
        desired_intent_id="stop:43", trigger_price=91, covered_quantity=2,
        closing_side="SELL", position_side="LONG", system="S6",
        entry_price=100, opened_at=4, operation_id="native-open:43", now=4,
    )
    assert reopened.authority.slot_generation == opened.authority.slot_generation + 1
    assert service.finalize(captured.fence, now=5).code is CloseFinalizeCode.STALE
    assert authority.get_slot_authority(_slot()).authority.status is AuthorityStatus.ACTIVE


def test_backend_read_failure_is_unavailable():
    class Down:
        def get_slot_authority(self, _key):
            return AuthorityReadResult(AuthorityReadCode.BACKEND_ERROR, message="down")

    service = CanonicalCloseFinalizer(authority_store=Down(), projection_store=object())
    assert service.capture(_slot(), local_position={}).code is CloseCaptureCode.UNAVAILABLE
