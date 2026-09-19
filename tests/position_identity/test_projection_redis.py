"""R1 deterministic projection Redis CAS tests without external services."""
import threading

from position_identity.projection import (
    LivePositionProjection,
    ProjectionReadCode,
    ProjectionWriteCode,
)
from position_identity.projection_redis import (
    COMPARE_AND_SET_PROJECTION_LUA,
    RedisLivePositionProjectionAdapter,
)
from position_identity.slot import ExchangePositionKey

EPISODE_A = '6a4717d8-7390-4386-9878-56ff2981f93d'
EPISODE_B = '887246d6-b2e6-4e04-8308-bc3a3577282c'


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id='trade-engine-demo', environment='DEMO',
        symbol='BTCUSDT')


def _projection(**overrides):
    values = {
        'exchange_position_key': _slot(), 'episode_id': EPISODE_A,
        'slot_generation': 3, 'identity_provenance': 'NATIVE',
        'state_revision': 1, 'last_operation_id': 'open-1',
        'side': 'LONG', 'system': 'S6', 'quantity': 2,
        'entry_price': 100, 'opened_at': 10, 'updated_at': 10,
    }
    values.update(overrides)
    return LivePositionProjection(**values)


class FakeProjectionRedis:
    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()
        self.raise_get = False
        self.raise_eval = False

    def get(self, key):
        if self.raise_get:
            raise RuntimeError('get unavailable')
        with self.lock:
            return self.data.get(key)

    def eval(self, script, numkeys, key, *args):
        if self.raise_eval:
            raise RuntimeError('ack lost')
        assert script == COMPARE_AND_SET_PROJECTION_LUA
        assert numkeys == 1
        operation, episode, generation, revision, operation_id, new_json = args
        proposed = LivePositionProjection.from_json(new_json)
        with self.lock:
            raw = self.data.get(key)
            if operation == 'CREATE':
                if raw is not None:
                    try:
                        current = LivePositionProjection.from_json(raw)
                    except (TypeError, ValueError):
                        return ['MALFORMED', raw]
                    if current.last_operation_id == operation_id \
                            and raw == new_json:
                        return ['ALREADY_APPLIED', raw]
                    return ['CONFLICT', raw]
                if proposed.state_revision != 1:
                    return ['INVALID', '']
                self.data[key] = new_json
                return ['APPLIED', new_json]
            if raw is None:
                return ['NOT_FOUND', '']
            try:
                current = LivePositionProjection.from_json(raw)
            except (TypeError, ValueError):
                return ['MALFORMED', raw]
            if current.last_operation_id == operation_id:
                code = 'ALREADY_APPLIED' if raw == new_json else 'CONFLICT'
                return [code, raw]
            if current.episode_id != episode \
                    or str(current.slot_generation) != generation \
                    or str(current.state_revision) != revision:
                return ['STALE', raw]
            if proposed.episode_id != episode \
                    or str(proposed.slot_generation) != generation \
                    or proposed.state_revision != current.state_revision + 1:
                return ['INVALID', '']
            self.data[key] = new_json
            return ['APPLIED', new_json]


def _adapter(fake):
    return RedisLivePositionProjectionAdapter(
        redis_get=fake.get, redis_eval=fake.eval)


def test_create_read_and_same_operation_retry_are_idempotent():
    fake = FakeProjectionRedis()
    adapter = _adapter(fake)
    projection = _projection()
    assert adapter.get(_slot()).code is ProjectionReadCode.NOT_FOUND
    first = adapter.create(projection)
    retry = adapter.create(projection)
    assert first.code is ProjectionWriteCode.APPLIED
    assert retry.code is ProjectionWriteCode.ALREADY_APPLIED
    assert adapter.get(_slot()).projection == projection


def test_cas_advances_revision_and_rejects_stale_writer():
    fake = FakeProjectionRedis()
    adapter = _adapter(fake)
    current = _projection()
    adapter.create(current)
    revised = current.revise(operation_id='update-2', now=20, quantity=3)
    applied = adapter.compare_and_set(
        revised, expected_episode_id=EPISODE_A,
        expected_slot_generation=3, expected_revision=1)
    stale = adapter.compare_and_set(
        current.revise(operation_id='stale', now=21, quantity=4),
        expected_episode_id=EPISODE_A,
        expected_slot_generation=3, expected_revision=1)
    assert applied.code is ProjectionWriteCode.APPLIED
    assert stale.code is ProjectionWriteCode.STALE
    assert adapter.get(_slot()).projection.quantity == 3


def test_two_concurrent_updates_have_one_winner():
    fake = FakeProjectionRedis()
    adapter = _adapter(fake)
    current = _projection()
    adapter.create(current)
    barrier = threading.Barrier(3)
    outcomes = []

    def update(operation, quantity):
        barrier.wait()
        outcomes.append(adapter.compare_and_set(
            current.revise(operation_id=operation, now=20, quantity=quantity),
            expected_episode_id=EPISODE_A,
            expected_slot_generation=3, expected_revision=1))

    threads = [
        threading.Thread(target=update, args=('a', 3)),
        threading.Thread(target=update, args=('b', 4)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    codes = [result.code for result in outcomes]
    assert codes.count(ProjectionWriteCode.APPLIED) == 1
    assert codes.count(ProjectionWriteCode.STALE) == 1


def test_episode_and_generation_aba_are_rejected():
    fake = FakeProjectionRedis()
    adapter = _adapter(fake)
    current = _projection()
    adapter.create(current)
    reopened = _projection(
        episode_id=EPISODE_B, slot_generation=4,
        last_operation_id='reopen', updated_at=30)
    fake.data[adapter._storage_key(_slot())] = reopened.to_json()
    stale = adapter.compare_and_set(
        current.revise(operation_id='old-a', now=31, quantity=5),
        expected_episode_id=EPISODE_A,
        expected_slot_generation=3, expected_revision=1)
    assert stale.code is ProjectionWriteCode.STALE
    assert adapter.get(_slot()).projection == reopened


def test_malformed_backend_and_ambiguous_ack_are_typed():
    fake = FakeProjectionRedis()
    adapter = _adapter(fake)
    key = adapter._storage_key(_slot())
    fake.data[key] = '{broken'
    assert adapter.get(_slot()).code is ProjectionReadCode.MALFORMED
    assert adapter.create(_projection()).code is ProjectionWriteCode.MALFORMED
    fake.raise_get = True
    assert adapter.get(_slot()).code is ProjectionReadCode.UNAVAILABLE
    fake.raise_get = False
    fake.raise_eval = True
    assert adapter.create(_projection()).code is ProjectionWriteCode.UNKNOWN


def test_lua_has_no_ttl_delete_positions_or_file_fallback():
    source = COMPARE_AND_SET_PROJECTION_LUA
    for token in (
        "current['episode_id']", "current['slot_generation']",
        "current['state_revision']", "current['last_operation_id']",
        "redis.call('SET', KEYS[1], new_json)",
    ):
        assert token in source
    for forbidden in ('EXPIRE', 'PEXPIRE', "redis.call('DEL'", 'pm:positions'):
        assert forbidden not in source
