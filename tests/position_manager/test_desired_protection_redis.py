"""R2 desired-protection Lua CAS tests on an isolated Redis process."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
import redis

from position_identity.slot import ExchangePositionKey
from position_protection.desired import (
    DesiredProtectionRecord,
    DesiredReadCode,
    DesiredWriteCode,
)
from position_protection.desired_redis import RedisDesiredProtectionAdapter

ROOT = Path(__file__).resolve().parents[2]
EPISODE_A = '6a4717d8-7390-4386-9878-56ff2981f93d'
EPISODE_B = '887246d6-b2e6-4e04-8308-bc3a3577282c'


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id='r2-isolated', environment='SANDBOX',
        symbol='BTCUSDT')


def _initial(*, episode=EPISODE_A, slot_generation=3,
             intent='initial-stop', trigger=90, operation='declare-1'):
    return DesiredProtectionRecord.initial_pending(
        exchange_position_key=_slot(), episode_id=episode,
        slot_generation=slot_generation, desired_intent_id=intent,
        trigger_price=trigger, covered_quantity=2, closing_side='SELL',
        operation_id=operation, now=10)


def _expected(record):
    return {
        'expected_episode_id': record.episode_id,
        'expected_slot_generation': record.slot_generation,
        'expected_protection_generation': record.protection_generation,
        'expected_revision': record.revision,
    }


@pytest.fixture(scope='module')
def isolated_redis(tmp_path_factory):
    executable = shutil.which('redis-server')
    if executable is None:
        pytest.skip('redis-server is not installed')
    directory = tmp_path_factory.mktemp('r2-redis')
    socket = directory / 'redis.sock'
    process = subprocess.Popen(
        [
            executable, '--port', '0', '--unixsocket', str(socket),
            '--unixsocketperm', '700', '--save', '', '--appendonly', 'no',
            '--dir', str(directory), '--daemonize', 'no',
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
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
    return RedisDesiredProtectionAdapter(
        redis_get=isolated_redis.get, redis_eval=isolated_redis.eval)


def test_create_read_and_exact_retry_are_idempotent(adapter):
    initial = _initial()
    assert adapter.get(_slot()).code is DesiredReadCode.NOT_FOUND
    assert adapter.declare(initial).code is DesiredWriteCode.APPLIED
    assert adapter.declare(initial).code is DesiredWriteCode.ALREADY_APPLIED
    assert adapter.get(_slot()).record == initial


def test_same_spec_retry_does_not_advance_generation(adapter):
    initial = _initial()
    assert adapter.declare(initial).applied
    duplicate_spec = replace(
        initial, desired_intent_id='same-value-new-request',
        protection_generation=2, revision=2,
        last_operation_id='declare-duplicate', updated_at=20)
    result = adapter.declare(duplicate_spec, **_expected(initial))
    assert result.code is DesiredWriteCode.ALREADY_APPLIED
    assert result.record == initial
    assert adapter.get(_slot()).record.protection_generation == 1


def test_changed_spec_advances_generation_once(adapter):
    initial = _initial()
    adapter.declare(initial)
    changed = initial.next_desired(
        desired_intent_id='break-even', trigger_price=100,
        covered_quantity=2, closing_side='SELL',
        operation_id='declare-2', now=20)
    assert adapter.declare(changed, **_expected(initial)).code \
        is DesiredWriteCode.APPLIED
    stale = initial.next_desired(
        desired_intent_id='trailing', trigger_price=95,
        covered_quantity=2, closing_side='SELL',
        operation_id='declare-stale', now=21)
    assert adapter.declare(stale, **_expected(initial)).code \
        is DesiredWriteCode.STALE
    assert adapter.get(_slot()).record == changed


def test_two_changed_specs_have_exactly_one_winner(adapter):
    initial = _initial()
    adapter.declare(initial)
    barrier = threading.Barrier(3)
    outcomes = []

    def declare(intent, trigger):
        candidate = initial.next_desired(
            desired_intent_id=intent, trigger_price=trigger,
            covered_quantity=2, closing_side='SELL',
            operation_id=f'op-{intent}', now=20)
        barrier.wait()
        outcomes.append(adapter.declare(
            candidate, **_expected(initial)).code)

    threads = [
        threading.Thread(target=declare, args=('break-even', 100)),
        threading.Thread(target=declare, args=('trail', 95)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert outcomes.count(DesiredWriteCode.APPLIED) == 1
    assert outcomes.count(DesiredWriteCode.STALE) == 1


def test_status_cas_does_not_advance_protection_generation(adapter):
    initial = _initial()
    adapter.declare(initial)
    submitting = initial.transition(
        status='SUBMITTING', operation_id='claim-1', now=20)
    result = adapter.compare_and_set(submitting, **_expected(initial))
    assert result.code is DesiredWriteCode.APPLIED
    assert result.record.protection_generation == 1
    unknown = submitting.transition(
        status='UNKNOWN', operation_id='ack-lost', now=21,
        exchange_algo_aliases=('algo-observed',))
    assert adapter.compare_and_set(
        unknown, **_expected(submitting)).code is DesiredWriteCode.APPLIED


def test_new_episode_resets_protection_generation_but_fences_old_episode(adapter):
    old = _initial()
    adapter.declare(old)
    new = _initial(
        episode=EPISODE_B, slot_generation=4,
        intent='episode-b-stop', operation='declare-b')
    assert adapter.declare(new, **_expected(old)).code \
        is DesiredWriteCode.APPLIED
    assert new.protection_generation == 1
    stale = old.transition(
        status='SUBMITTING', operation_id='old-worker', now=30)
    assert adapter.compare_and_set(stale, **_expected(old)).code \
        is DesiredWriteCode.STALE
    assert adapter.get(_slot()).record == new


@pytest.mark.parametrize('raw', [
    '{broken',
    '{"schema_version":1}',
    json.dumps({**_initial().to_dict(), 'status': 'PROTECTED'}),
])
def test_malformed_records_are_never_overwritten(
        adapter, isolated_redis, raw):
    key = adapter._storage_key(_slot())
    isolated_redis.set(key, raw)
    before = isolated_redis.get(key)
    assert adapter.get(_slot()).code is DesiredReadCode.MALFORMED
    assert adapter.declare(_initial()).code is DesiredWriteCode.MALFORMED
    assert isolated_redis.get(key) == before


def test_unavailable_unknown_and_invalid_expectations_are_distinct():
    calls = []
    unavailable = RedisDesiredProtectionAdapter(
        redis_get=lambda _key: None,
        redis_eval=lambda *args: calls.append(args),
        redis_available=lambda: False).declare(_initial())
    assert unavailable.code is DesiredWriteCode.UNAVAILABLE
    assert calls == []

    def fail_eval(*_args):
        raise RuntimeError('ack lost')

    unknown = RedisDesiredProtectionAdapter(
        redis_get=lambda _key: None, redis_eval=fail_eval,
        redis_available=lambda: True).declare(_initial())
    assert unknown.code is DesiredWriteCode.UNKNOWN

    invalid = RedisDesiredProtectionAdapter(
        redis_get=lambda _key: None,
        redis_eval=lambda *args: calls.append(args)).declare(
            _initial(), expected_episode_id=EPISODE_A)
    assert invalid.code is DesiredWriteCode.INVALID
    assert calls == []


def test_adapter_import_is_io_free_in_clean_subprocess():
    code = (
        'import json, sys; import position_protection.desired_redis; '
        'blocked=("redis", "requests", "shared.position_manager", '
        '"position_runtime.runtime", "strategies.shared_executor"); '
        'print(json.dumps([name for name in blocked if name in sys.modules]))'
    )
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT)
    result = subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, env=env,
        check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == []
    assert result.stderr == ''


def test_namespace_and_lua_have_no_ttl_delete_snapshot_or_file_fallback():
    source = (ROOT / 'position_protection/desired_redis.py').read_text()
    assert "STORAGE_KEY_PREFIX = 'pm:desired-protection:v1'" in source
    for token in (
        "current['episode_id']", "current['slot_generation']",
        "current['protection_generation']", "current['revision']",
        "redis.call('SET', KEYS[1], new_json)",
    ):
        assert token in source
    for forbidden in (
        'pm:positions', 'PEXPIRE', 'EXPIRE', 'SETEX',
        "redis.call('DEL'", 'Path(', 'open(', 'write_text',
    ):
        assert forbidden not in source
