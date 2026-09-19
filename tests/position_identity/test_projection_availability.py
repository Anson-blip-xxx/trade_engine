"""R1 pre-attempt unavailable versus post-attempt unknown semantics."""
from position_identity.projection import (
    LivePositionProjection,
    ProjectionWriteCode,
)
from position_identity.projection_redis import RedisLivePositionProjectionAdapter
from position_identity.slot import ExchangePositionKey


def _projection():
    return LivePositionProjection(
        exchange_position_key=ExchangePositionKey.one_way(
            account_principal_id='availability-test', environment='SANDBOX',
            symbol='BTCUSDT'),
        episode_id='6a4717d8-7390-4386-9878-56ff2981f93d',
        slot_generation=1, identity_provenance='NATIVE',
        state_revision=1, last_operation_id='open-1', side='LONG',
        system='S6', quantity=1, entry_price=100,
        opened_at=1, updated_at=1,
    )


def test_known_pre_attempt_outage_is_unavailable_and_never_evaluates():
    eval_calls = []
    adapter = RedisLivePositionProjectionAdapter(
        redis_get=lambda _key: None,
        redis_eval=lambda *args: eval_calls.append(args),
        redis_available=lambda: False,
    )
    result = adapter.create(_projection())
    assert result.code is ProjectionWriteCode.UNAVAILABLE
    assert not result.applied
    assert eval_calls == []


def test_probe_error_is_unavailable_but_eval_error_is_unknown():
    def fail_probe():
        raise RuntimeError('pool known down')

    pre_attempt = RedisLivePositionProjectionAdapter(
        redis_get=lambda _key: None, redis_eval=lambda *_args: None,
        redis_available=fail_probe,
    ).create(_projection())

    def fail_eval(*_args):
        raise RuntimeError('ack lost after send')

    post_attempt = RedisLivePositionProjectionAdapter(
        redis_get=lambda _key: None, redis_eval=fail_eval,
        redis_available=lambda: True,
    ).create(_projection())
    assert pre_attempt.code is ProjectionWriteCode.UNAVAILABLE
    assert post_attempt.code is ProjectionWriteCode.UNKNOWN
