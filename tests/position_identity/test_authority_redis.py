"""P10-07C deterministic Redis-CAS adapter tests without external services."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from position_identity import (
    AuthorityAckCode,
    AuthorityProvenance,
    AuthorityReadCode,
    AuthorityStatus,
    ExchangePositionKey,
    RedisSlotAuthorityAdapter,
    SlotAuthority,
)
from position_identity.authority_redis import COMPARE_AND_TRANSITION_SLOT_LUA

ROOT = Path(__file__).resolve().parents[2]


def test_lua_compares_json_null_with_cjson_null():
    assert 'current_episode ~= cjson.null' in COMPARE_AND_TRANSITION_SLOT_LUA
NULL_EPISODE = '__P10_NULL_EPISODE__'


def _key(symbol='BTCUSDT'):
    return ExchangePositionKey.one_way(
        account_principal_id='binance-main',
        environment='PROD',
        symbol=symbol,
    )


class FakeAuthorityRedis:
    """Atomic in-memory implementation of the injected Lua command contract."""

    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()
        self.raise_get = False
        self.raise_eval = False
        self.eval_calls = []

    def get(self, key):
        if self.raise_get:
            raise RuntimeError('get unavailable')
        with self.lock:
            return self.data.get(key)

    def eval(self, script, numkeys, key, *args):
        if self.raise_eval:
            raise RuntimeError('eval unavailable')
        assert script == COMPARE_AND_TRANSITION_SLOT_LUA
        assert numkeys == 1
        operation, expected_revision, expected_status, expected_episode, \
            expected_generation, new_json = args
        self.eval_calls.append((key, operation))
        with self.lock:
            raw = self.data.get(key)
            try:
                SlotAuthority.from_json(new_json)
            except (TypeError, ValueError):
                return ['MALFORMED', '']

            if operation == 'INIT':
                if raw is not None:
                    try:
                        current = SlotAuthority.from_json(raw)
                    except (TypeError, ValueError):
                        return ['MALFORMED', raw]
                    code = ('ALREADY_ACTIVE'
                            if current.status in (
                                AuthorityStatus.ACTIVE,
                                AuthorityStatus.QUARANTINED)
                            else 'ALREADY_INITIALIZED')
                    return [code, raw]
                self.data[key] = new_json
                return ['APPLIED', new_json]

            if raw is None:
                return ['NOT_FOUND', '']
            try:
                current = SlotAuthority.from_json(raw)
            except (TypeError, ValueError):
                return ['MALFORMED', raw]

            actual_episode = current.episode_id or NULL_EPISODE
            if str(current.revision) != expected_revision \
                    or str(current.slot_generation) != expected_generation \
                    or actual_episode != expected_episode:
                return ['CONFLICT', raw]
            if current.status.value != expected_status:
                return ['INVALID_STATE', raw]
            self.data[key] = new_json
            return ['APPLIED', new_json]


def _adapter(fake):
    return RedisSlotAuthorityAdapter(
        redis_get=fake.get,
        redis_eval=fake.eval,
    )


def _initialize(adapter, key=None):
    key = key or _key()
    ack = adapter.initialize_flat(key, now=10)
    assert ack.code is AuthorityAckCode.APPLIED
    return key, ack.authority


def test_read_missing_initialize_and_round_trip():
    fake = FakeAuthorityRedis()
    adapter = _adapter(fake)
    key = _key()
    assert adapter.get_slot_authority(key).code is AuthorityReadCode.NOT_FOUND
    ack = adapter.initialize_flat(key, now=10)
    assert ack.code is AuthorityAckCode.APPLIED
    assert ack.authority.status is AuthorityStatus.FLAT
    result = adapter.get_slot_authority(key)
    assert result.code is AuthorityReadCode.FOUND
    assert result.authority == ack.authority


def test_initialize_is_atomic_create_if_absent():
    fake = FakeAuthorityRedis()
    adapter = _adapter(fake)
    key, winner = _initialize(adapter)
    duplicate = adapter.initialize_flat(key, now=11)
    assert duplicate.code is AuthorityAckCode.ALREADY_INITIALIZED
    assert duplicate.authority == winner


def test_allocate_new_episode_advances_generation_and_revision():
    fake = FakeAuthorityRedis()
    adapter = _adapter(fake)
    key, flat = _initialize(adapter)
    ack = adapter.allocate_new_episode(
        key,
        candidate_episode_id='episode-A',
        expected_revision=flat.revision,
        expected_slot_generation=flat.slot_generation,
        expected_episode_id=flat.episode_id,
        now=20,
    )
    assert ack.code is AuthorityAckCode.APPLIED
    assert ack.authority.episode_id == 'episode-A'
    assert ack.authority.slot_generation == 1
    assert ack.authority.revision == 2
    assert ack.authority.provenance is AuthorityProvenance.NATIVE


def test_two_concurrent_candidates_have_exactly_one_winner():
    fake = FakeAuthorityRedis()
    adapter = _adapter(fake)
    key, flat = _initialize(adapter)
    barrier = threading.Barrier(3)
    outcomes = []

    def allocate(candidate):
        barrier.wait()
        outcomes.append(adapter.allocate_new_episode(
            key,
            candidate_episode_id=candidate,
            expected_revision=flat.revision,
            expected_slot_generation=flat.slot_generation,
            expected_episode_id=None,
            now=20,
        ))

    threads = [
        threading.Thread(target=allocate, args=('episode-A',)),
        threading.Thread(target=allocate, args=('episode-B',)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert [item.code for item in outcomes].count(AuthorityAckCode.APPLIED) == 1
    assert [item.code for item in outcomes].count(AuthorityAckCode.CONFLICT) == 1
    winner = adapter.get_slot_authority(key).authority
    assert winner.episode_id in ('episode-A', 'episode-B')
    assert winner.slot_generation == 1
    assert {item.authority.episode_id for item in outcomes} == {
        winner.episode_id,
    }


def test_stale_revision_generation_and_episode_are_rejected():
    fake = FakeAuthorityRedis()
    adapter = _adapter(fake)
    key, _flat = _initialize(adapter)
    applied = adapter.allocate_new_episode(
        key,
        candidate_episode_id='episode-A',
        expected_revision=1,
        expected_slot_generation=0,
        expected_episode_id=None,
        now=20,
    )
    assert applied.applied
    for revision, generation, episode in (
        (1, 1, 'episode-A'),
        (2, 0, 'episode-A'),
        (2, 1, 'episode-X'),
    ):
        ack = adapter.transition_active_to_flat(
            key,
            expected_episode_id=episode,
            expected_slot_generation=generation,
            expected_revision=revision,
            now=30,
        )
        assert ack.code is AuthorityAckCode.CONFLICT
    assert adapter.get_slot_authority(key).authority == applied.authority


def test_active_to_flat_retains_episode_generation_and_high_water():
    fake = FakeAuthorityRedis()
    adapter = _adapter(fake)
    key, flat = _initialize(adapter)
    active = adapter.allocate_new_episode(
        key,
        candidate_episode_id='episode-A',
        expected_revision=flat.revision,
        expected_slot_generation=flat.slot_generation,
        expected_episode_id=None,
        now=20,
    ).authority
    ack = adapter.transition_active_to_flat(
        key,
        expected_episode_id='episode-A',
        expected_slot_generation=1,
        expected_revision=2,
        now=30,
    )
    assert ack.code is AuthorityAckCode.APPLIED
    assert ack.authority.status is AuthorityStatus.FLAT
    assert ack.authority.episode_id == active.episode_id
    assert ack.authority.slot_generation == 1
    assert ack.authority.revision == 3
    assert key.to_storage_key() in fake.data


def test_double_flat_is_rejected_with_typed_result():
    fake = FakeAuthorityRedis()
    adapter = _adapter(fake)
    key, flat = _initialize(adapter)
    active = adapter.allocate_new_episode(
        key,
        candidate_episode_id='episode-A',
        expected_revision=flat.revision,
        expected_slot_generation=flat.slot_generation,
        expected_episode_id=None,
        now=20,
    ).authority
    first = adapter.transition_active_to_flat(
        key,
        expected_episode_id=active.episode_id,
        expected_slot_generation=active.slot_generation,
        expected_revision=active.revision,
        now=30,
    )
    second = adapter.transition_active_to_flat(
        key,
        expected_episode_id=active.episode_id,
        expected_slot_generation=active.slot_generation,
        expected_revision=first.authority.revision,
        now=31,
    )
    assert first.code is AuthorityAckCode.APPLIED
    assert second.code is AuthorityAckCode.INVALID_STATE


def test_reopen_increments_generation_and_old_episode_cannot_mutate_b():
    fake = FakeAuthorityRedis()
    adapter = _adapter(fake)
    key, flat0 = _initialize(adapter)
    active_a = adapter.allocate_new_episode(
        key, candidate_episode_id='episode-A', expected_revision=1,
        expected_slot_generation=0, expected_episode_id=None, now=20,
    ).authority
    flat_a = adapter.transition_active_to_flat(
        key, expected_episode_id='episode-A', expected_slot_generation=1,
        expected_revision=2, now=30,
    ).authority
    active_b = adapter.allocate_new_episode(
        key, candidate_episode_id='episode-B', expected_revision=3,
        expected_slot_generation=1, expected_episode_id='episode-A', now=40,
    ).authority
    stale_a = adapter.transition_active_to_flat(
        key, expected_episode_id=active_a.episode_id,
        expected_slot_generation=active_a.slot_generation,
        expected_revision=active_a.revision, now=50,
    )
    assert flat0.slot_generation == 0
    assert flat_a.slot_generation == 1
    assert active_b.slot_generation == 2
    assert active_b.episode_id == 'episode-B'
    assert stale_a.code is AuthorityAckCode.CONFLICT
    assert adapter.get_slot_authority(key).authority == active_b


def test_controlled_adoption_has_one_winner_and_preserves_legacy_alias():
    fake = FakeAuthorityRedis()
    adapter = _adapter(fake)
    key, flat = _initialize(adapter)
    winner = adapter.adopt_if_unowned(
        key,
        candidate_episode_id='migrated-A',
        legacy_position_id_alias='S6:BTCUSDT:legacy',
        expected_revision=flat.revision,
        expected_slot_generation=flat.slot_generation,
        expected_episode_id=None,
        now=20,
    )
    loser = adapter.adopt_if_unowned(
        key,
        candidate_episode_id='migrated-B',
        legacy_position_id_alias='S6:BTCUSDT:other',
        expected_revision=flat.revision,
        expected_slot_generation=flat.slot_generation,
        expected_episode_id=None,
        now=20,
    )
    assert winner.code is AuthorityAckCode.APPLIED
    assert winner.authority.provenance is AuthorityProvenance.MIGRATED
    assert winner.authority.legacy_position_id_alias == 'S6:BTCUSDT:legacy'
    assert loser.code is AuthorityAckCode.CONFLICT
    assert loser.authority == winner.authority


def test_reconstructed_creation_is_quarantined_not_active():
    fake = FakeAuthorityRedis()
    adapter = _adapter(fake)
    key, flat = _initialize(adapter)
    ack = adapter.create_reconstructed_quarantined(
        key,
        candidate_episode_id='reconstructed-A',
        legacy_position_id_alias=None,
        expected_revision=flat.revision,
        expected_slot_generation=flat.slot_generation,
        expected_episode_id=None,
        now=20,
    )
    assert ack.code is AuthorityAckCode.APPLIED
    assert ack.authority.status is AuthorityStatus.QUARANTINED
    assert ack.authority.provenance is AuthorityProvenance.RECONSTRUCTED
    ordinary = adapter.allocate_new_episode(
        key,
        candidate_episode_id='native-B',
        expected_revision=ack.authority.revision,
        expected_slot_generation=ack.authority.slot_generation,
        expected_episode_id=ack.authority.episode_id,
        now=30,
    )
    assert ordinary.code is AuthorityAckCode.ALREADY_ACTIVE


def test_malformed_and_unknown_schema_are_not_overwritten():
    fake = FakeAuthorityRedis()
    adapter = _adapter(fake)
    key = _key()
    storage_key = key.to_storage_key()
    for raw in ('{broken', json.dumps({'schema_version': 2})):
        fake.data[storage_key] = raw
        before = fake.data[storage_key]
        read = adapter.get_slot_authority(key)
        init = adapter.initialize_flat(key, now=10)
        assert read.code is AuthorityReadCode.MALFORMED
        assert init.code is AuthorityAckCode.MALFORMED
        assert fake.data[storage_key] == before


def test_backend_get_and_eval_errors_are_distinct():
    fake = FakeAuthorityRedis()
    adapter = _adapter(fake)
    key = _key()
    fake.raise_get = True
    assert adapter.get_slot_authority(key).code is AuthorityReadCode.BACKEND_ERROR
    fake.raise_get = False
    fake.raise_eval = True
    ack = adapter.initialize_flat(key, now=10)
    assert ack.code is AuthorityAckCode.BACKEND_ERROR
    assert 'eval unavailable' in ack.message


def test_invalid_utf8_cas_response_is_backend_error():
    ack = RedisSlotAuthorityAdapter._parse_ack([b'\xff', b''], _key())
    assert ack.code is AuthorityAckCode.BACKEND_ERROR
    assert 'UTF-8' in ack.message


def test_lua_uses_cas_set_without_lock_ttl_delete_or_positions_key():
    script = COMPARE_AND_TRANSITION_SLOT_LUA
    assert "redis.call('GET', KEYS[1])" in script
    assert "redis.call('SET', KEYS[1], new_json)" in script
    assert "current['revision']" in script
    assert "current['slot_generation']" in script
    assert "current['episode_id']" in script
    assert "current['status']" in script
    for token in ('PEXPIRE', 'EXPIRE', "redis.call('DEL'", 'pm:positions',
                  'lock_acquire', 'lock_renew'):
        assert token not in script


def test_adapter_import_is_io_free_in_clean_subprocess():
    code = (
        'import json, sys; import position_identity.authority_redis; '
        'blocked=("redis", "requests", "shared.position_manager", '
        '"position_protection.service", "position_reconcile.service"); '
        'print(json.dumps([name for name in blocked if name in sys.modules]))'
    )
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT)
    result = subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, env=env,
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == []
    assert result.stderr == ''
