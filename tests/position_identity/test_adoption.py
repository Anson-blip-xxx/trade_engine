"""P10-07D dormant legacy-adoption and reconstruction tests."""
import dataclasses
import threading
from decimal import Decimal

import pytest

from position_identity import (
    AdoptionClassification,
    AuthorityAck,
    AuthorityAckCode,
    AuthorityProvenance,
    AuthorityReadCode,
    AuthorityReadResult,
    AuthorityStatus,
    ExchangePositionKey,
    OnboardingResultCode,
    QuarantineReason,
    RedisSlotAuthorityAdapter,
    SlotAuthority,
    apply_legacy_adoption,
    apply_reconstructed_episode,
    can_mutate_async,
    classify_legacy_active,
    classify_reconstructed_exposure,
    prepare_legacy_adoption,
    prepare_reconstructed_episode,
)
from position_identity.authority_redis import COMPARE_AND_TRANSITION_SLOT_LUA

NULL_EPISODE = '__P10_NULL_EPISODE__'
LEGACY_A = '10000000-0000-4000-8000-000000000001'
LEGACY_B = '10000000-0000-4000-8000-000000000002'
NATIVE_A = '20000000-0000-4000-8000-000000000001'
RECONSTRUCTED_B = '30000000-0000-4000-8000-000000000001'


def _key(symbol='BTCUSDT'):
    return ExchangePositionKey.one_way(
        account_principal_id='binance-main',
        environment='PROD',
        symbol=symbol,
    )


class FakeAuthorityRedis:
    """Thread-safe implementation of the injected Redis Lua contract."""

    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()
        self.raise_get = False
        self.raise_eval = False
        self.get_calls = 0
        self.eval_calls = 0

    def get(self, key):
        self.get_calls += 1
        if self.raise_get:
            raise RuntimeError('get unavailable')
        with self.lock:
            return self.data.get(key)

    def eval(self, script, numkeys, key, *args):
        self.eval_calls += 1
        if self.raise_eval:
            raise RuntimeError('eval unavailable')
        assert script == COMPARE_AND_TRANSITION_SLOT_LUA
        assert numkeys == 1
        operation, expected_revision, expected_status, expected_episode, \
            expected_generation, new_json = args
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


def _adapter(fake=None):
    fake = fake or FakeAuthorityRedis()
    return fake, RedisSlotAuthorityAdapter(
        redis_get=fake.get,
        redis_eval=fake.eval,
    )


def _legacy_plan(
        adapter, key, candidate=LEGACY_A, alias='legacy:S6:BTCUSDT',
        **overrides):
    values = {
        'slot_key': key,
        'candidate_episode_id': candidate,
        'legacy_position_id_alias': alias,
        'local_symbol': key.symbol,
        'local_side': 'LONG',
        'local_quantity': '1.0004',
        'exchange_side': 'LONG',
        'exchange_quantity': '1.0000',
        'quantity_tolerance': '0.001',
        'authority_read': adapter.get_slot_authority(key),
        'mixed_version': False,
    }
    values.update(overrides)
    return prepare_legacy_adoption(**values)


def _reconstruction_plan(adapter, key, candidate=RECONSTRUCTED_B, **overrides):
    values = {
        'slot_key': key,
        'candidate_episode_id': candidate,
        'exchange_side': 'LONG',
        'exchange_quantity': '1.25',
        'authority_read': adapter.get_slot_authority(key),
    }
    values.update(overrides)
    return prepare_reconstructed_episode(**values)


def _initialize(adapter, key=None, now=10):
    key = key or _key()
    ack = adapter.initialize_flat(key, now=now)
    assert ack.code is AuthorityAckCode.APPLIED
    return key, ack.authority


def test_consistent_legacy_evidence_is_adoptable_with_decimal_tolerance():
    _, adapter = _adapter()
    key, flat = _initialize(adapter)
    plan = _legacy_plan(adapter, key)
    assert plan.classification is AdoptionClassification.LEGACY_ADOPTABLE
    assert plan.local_quantity == Decimal('1.0004')
    assert plan.exchange_quantity == Decimal('1.0000')
    assert plan.quantity_tolerance == Decimal('0.001')
    assert plan.expected_authority.revision == flat.revision
    assert plan.candidate_slot_generation == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.reason = QuarantineReason.AUTHORITY_CONFLICT


def test_classification_api_is_pure_and_typed():
    fake, adapter = _adapter()
    key, _flat = _initialize(adapter)
    before = fake.eval_calls
    decision = classify_legacy_active(
        slot_key=key,
        legacy_position_id_alias='legacy:S6:BTCUSDT',
        local_symbol='BTCUSDT',
        local_side='LONG',
        local_quantity='1',
        exchange_side='LONG',
        exchange_quantity='1',
        quantity_tolerance='0.001',
        authority_read=adapter.get_slot_authority(key),
        mixed_version=False,
    )
    assert decision.classification is AdoptionClassification.LEGACY_ADOPTABLE
    assert fake.eval_calls == before


def test_missing_account_quarantines_without_guessing_slot():
    _, adapter = _adapter()
    plan = prepare_legacy_adoption(
        slot_key=None,
        candidate_episode_id=LEGACY_A,
        legacy_position_id_alias='legacy-id',
        local_symbol='BTCUSDT',
        local_side='LONG',
        local_quantity='1',
        exchange_side='LONG',
        exchange_quantity='1',
        quantity_tolerance='0.001',
        authority_read=adapter.get_slot_authority(_key()),
        mixed_version=False,
    )
    assert plan.classification is AdoptionClassification.LEGACY_QUARANTINE
    assert plan.reason is QuarantineReason.ACCOUNT_UNKNOWN
    result = apply_legacy_adoption(plan, adapter, now=20)
    assert result.code is OnboardingResultCode.QUARANTINED
    assert result.reason is QuarantineReason.ACCOUNT_UNKNOWN


@pytest.mark.parametrize(('override', 'reason'), [
    ({'exchange_quantity': '1.1'}, QuarantineReason.EXCHANGE_LOCAL_MISMATCH),
    ({'exchange_side': 'SHORT'}, QuarantineReason.EXCHANGE_LOCAL_MISMATCH),
    ({'local_symbol': 'ETHUSDT'}, QuarantineReason.EXCHANGE_LOCAL_MISMATCH),
    ({'mixed_version': True}, QuarantineReason.MIXED_VERSION),
])
def test_unsafe_legacy_evidence_is_quarantined(override, reason):
    _, adapter = _adapter()
    key, _flat = _initialize(adapter)
    plan = _legacy_plan(adapter, key, **override)
    assert plan.classification is AdoptionClassification.LEGACY_QUARANTINE
    assert plan.reason is reason


@pytest.mark.parametrize('override', [
    {'local_symbol': ''},
    {'local_quantity': 'NaN'},
    {'local_quantity': object()},
    {'legacy_position_id_alias': None},
    {'candidate_episode_id': 'not-a-uuid'},
])
def test_malformed_legacy_input_is_invalid_and_never_writes(override):
    fake, adapter = _adapter()
    key, _flat = _initialize(adapter)
    before = fake.eval_calls
    plan = _legacy_plan(adapter, key, **override)
    result = apply_legacy_adoption(plan, adapter, now=20)
    assert plan.classification is AdoptionClassification.INVALID
    assert result.code is OnboardingResultCode.INVALID
    assert fake.eval_calls == before


def test_missing_authority_requires_explicit_bootstrap_authorization():
    _, adapter = _adapter()
    key = _key()
    denied = _legacy_plan(adapter, key)
    allowed = _legacy_plan(
        adapter, key, allow_authority_initialization=True)
    assert denied.classification is AdoptionClassification.LEGACY_QUARANTINE
    assert denied.reason is QuarantineReason.AUTHORITY_MISSING
    assert allowed.classification is AdoptionClassification.LEGACY_ADOPTABLE
    assert allowed.requires_authority_initialization is True
    assert allowed.candidate_slot_generation == 1


def test_successful_fresh_slot_adoption_is_migrated_active_with_alias():
    _, adapter = _adapter()
    key = _key()
    plan = _legacy_plan(
        adapter, key, allow_authority_initialization=True)
    result = apply_legacy_adoption(plan, adapter, now=20)
    assert result.code is OnboardingResultCode.ADOPTED
    assert result.authority.status is AuthorityStatus.ACTIVE
    assert result.authority.provenance is AuthorityProvenance.MIGRATED
    assert result.authority.episode_id == LEGACY_A
    assert result.authority.legacy_position_id_alias == 'legacy:S6:BTCUSDT'
    assert result.authority.episode_id \
        != result.authority.legacy_position_id_alias
    assert result.authority.slot_generation == 1


def test_existing_active_owner_is_typed_conflict():
    _, adapter = _adapter()
    key, flat = _initialize(adapter)
    active = adapter.allocate_new_episode(
        key,
        candidate_episode_id=NATIVE_A,
        expected_revision=flat.revision,
        expected_slot_generation=flat.slot_generation,
        expected_episode_id=flat.episode_id,
        now=20,
    ).authority
    plan = _legacy_plan(adapter, key)
    assert plan.classification is AdoptionClassification.NATIVE_AUTHORIZED
    result = apply_legacy_adoption(plan, adapter, now=30)
    assert result.code is OnboardingResultCode.EXISTING_OWNER
    assert result.authority == active
    assert result.candidate_discarded is True


def test_authority_read_for_another_slot_is_invalid():
    _, adapter = _adapter()
    btc, _btc_flat = _initialize(adapter, _key('BTCUSDT'))
    eth, _eth_flat = _initialize(adapter, _key('ETHUSDT'))
    wrong_read = adapter.get_slot_authority(eth)
    legacy = _legacy_plan(adapter, btc, authority_read=wrong_read)
    reconstruction = _reconstruction_plan(
        adapter, btc, authority_read=wrong_read)
    assert legacy.classification is AdoptionClassification.INVALID
    assert legacy.reason is QuarantineReason.MALFORMED_AUTHORITY
    assert reconstruction.classification is AdoptionClassification.INVALID
    assert reconstruction.reason is QuarantineReason.MALFORMED_AUTHORITY


def test_malformed_authority_read_shape_is_invalid():
    _, adapter = _adapter()
    key, flat = _initialize(adapter)
    malformed = AuthorityReadResult('FOUND', flat)
    legacy = _legacy_plan(adapter, key, authority_read=malformed)
    reconstruction = _reconstruction_plan(
        adapter, key, authority_read=malformed)
    assert legacy.classification is AdoptionClassification.INVALID
    assert reconstruction.classification is AdoptionClassification.INVALID


def test_two_concurrent_legacy_adopters_have_one_canonical_winner():
    _, adapter = _adapter()
    key, _flat = _initialize(adapter)
    plans = [
        _legacy_plan(adapter, key, candidate=LEGACY_A, alias='legacy-A'),
        _legacy_plan(adapter, key, candidate=LEGACY_B, alias='legacy-B'),
    ]
    barrier = threading.Barrier(3)
    results = []

    def apply(plan):
        barrier.wait()
        results.append(apply_legacy_adoption(plan, adapter, now=20))

    threads = [threading.Thread(target=apply, args=(plan,)) for plan in plans]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert [result.code for result in results].count(
        OnboardingResultCode.ADOPTED) == 1
    assert [result.code for result in results].count(
        OnboardingResultCode.CONFLICT) == 1
    winner = adapter.get_slot_authority(key).authority
    loser = next(result for result in results
                 if result.code is OnboardingResultCode.CONFLICT)
    assert loser.authority == winner
    assert loser.candidate_discarded is True
    assert winner.episode_id in (LEGACY_A, LEGACY_B)


def test_legacy_reentry_converges_without_new_generation_or_write():
    fake, adapter = _adapter()
    key, _flat = _initialize(adapter)
    first = apply_legacy_adoption(_legacy_plan(adapter, key), adapter, now=20)
    before = fake.eval_calls
    reentry = _legacy_plan(adapter, key, candidate=LEGACY_B)
    result = apply_legacy_adoption(reentry, adapter, now=30)
    assert reentry.classification is AdoptionClassification.ALREADY_ADOPTED
    assert result.code is OnboardingResultCode.ALREADY_ADOPTED
    assert result.authority == first.authority
    assert result.authority.slot_generation == 1
    assert fake.eval_calls == before
    assert result.candidate_discarded is True


def test_stale_fresh_slot_plan_does_not_rebase_after_closed_episode():
    fake, adapter = _adapter()
    key = _key()
    stale = _legacy_plan(
        adapter, key, allow_authority_initialization=True)
    _key_value, flat = _initialize(adapter, key)
    active = adapter.allocate_new_episode(
        key,
        candidate_episode_id=NATIVE_A,
        expected_revision=flat.revision,
        expected_slot_generation=flat.slot_generation,
        expected_episode_id=flat.episode_id,
        now=20,
    ).authority
    closed = adapter.transition_active_to_flat(
        key,
        expected_episode_id=active.episode_id,
        expected_slot_generation=active.slot_generation,
        expected_revision=active.revision,
        now=30,
    ).authority
    before = fake.eval_calls
    result = apply_legacy_adoption(stale, adapter, now=40)
    assert result.code is OnboardingResultCode.CONFLICT
    assert result.authority == closed
    assert adapter.get_slot_authority(key).authority == closed
    assert fake.eval_calls == before + 1


def test_forged_adoptable_plan_with_missing_evidence_never_writes():
    fake, adapter = _adapter()
    key, _flat = _initialize(adapter)
    plan = _legacy_plan(adapter, key)
    forged = dataclasses.replace(plan, local_quantity=None)
    before = fake.eval_calls
    result = apply_legacy_adoption(forged, adapter, now=20)
    assert result.code is OnboardingResultCode.INVALID
    assert fake.eval_calls == before


def test_forged_success_classification_is_rejected():
    _, adapter = _adapter()
    key, _flat = _initialize(adapter)
    legacy = _legacy_plan(adapter, key)
    forged_legacy = dataclasses.replace(
        legacy, classification=AdoptionClassification.ALREADY_ADOPTED)
    reconstructed = _reconstruction_plan(adapter, key)
    forged_reconstructed = dataclasses.replace(
        reconstructed,
        classification=AdoptionClassification.ALREADY_RECONSTRUCTED,
    )
    assert apply_legacy_adoption(
        forged_legacy, adapter, now=20).code is OnboardingResultCode.INVALID
    assert apply_reconstructed_episode(
        forged_reconstructed, adapter,
        now=20,
    ).code is OnboardingResultCode.INVALID


def test_forged_failure_classification_is_rejected():
    _, adapter = _adapter()
    key, _flat = _initialize(adapter)
    plan = _legacy_plan(adapter, key)
    forged_backend = dataclasses.replace(
        plan,
        classification=AdoptionClassification.CONFLICT,
        reason=QuarantineReason.BACKEND_UNAVAILABLE,
    )
    forged_quarantine = dataclasses.replace(
        plan,
        classification=AdoptionClassification.LEGACY_QUARANTINE,
        reason=QuarantineReason.EXCHANGE_LOCAL_MISMATCH,
    )
    assert apply_legacy_adoption(
        forged_backend, adapter, now=20).code is OnboardingResultCode.INVALID
    assert apply_legacy_adoption(
        forged_quarantine, adapter, now=20).code is OnboardingResultCode.INVALID


def test_forged_plan_slot_type_is_rejected_without_store_call():
    fake, adapter = _adapter()
    key, _flat = _initialize(adapter)
    forged = dataclasses.replace(_legacy_plan(adapter, key), slot_key=object())
    before_get = fake.get_calls
    before_eval = fake.eval_calls
    result = apply_legacy_adoption(forged, adapter, now=20)
    assert result.code is OnboardingResultCode.INVALID
    assert fake.get_calls == before_get
    assert fake.eval_calls == before_eval


def test_malformed_applied_ack_is_not_accepted_as_success():
    _, adapter = _adapter()
    key, _flat = _initialize(adapter)
    plan = _legacy_plan(adapter, key)

    class BadAppliedStore:
        get_slot_authority = adapter.get_slot_authority
        initialize_flat = adapter.initialize_flat

        @staticmethod
        def adopt_if_unowned(*args, **kwargs):
            return AuthorityAck(AuthorityAckCode.APPLIED)

    result = apply_legacy_adoption(plan, BadAppliedStore(), now=20)
    assert result.code is OnboardingResultCode.INVALID
    assert result.reason is QuarantineReason.MALFORMED_AUTHORITY
    assert adapter.get_slot_authority(key).authority.status is AuthorityStatus.FLAT


def test_malformed_cas_reload_is_typed_invalid():
    _, adapter = _adapter()
    key, _flat = _initialize(adapter)
    plan = _legacy_plan(adapter, key)

    class MalformedReloadStore:
        initialize_flat = adapter.initialize_flat

        @staticmethod
        def adopt_if_unowned(*args, **kwargs):
            return AuthorityAck(AuthorityAckCode.CONFLICT)

        @staticmethod
        def get_slot_authority(key):
            return AuthorityReadResult(AuthorityReadCode.FOUND, object())

    result = apply_legacy_adoption(plan, MalformedReloadStore(), now=20)
    assert result.code is OnboardingResultCode.INVALID
    assert result.reason is QuarantineReason.MALFORMED_AUTHORITY


def test_unknown_backend_ack_reloads_committed_candidate():
    _, adapter = _adapter()
    key, _flat = _initialize(adapter)
    plan = _legacy_plan(adapter, key)

    class CommitThenUnknownStore:
        get_slot_authority = adapter.get_slot_authority
        initialize_flat = adapter.initialize_flat

        @staticmethod
        def adopt_if_unowned(*args, **kwargs):
            applied = adapter.adopt_if_unowned(*args, **kwargs)
            assert applied.code is AuthorityAckCode.APPLIED
            return AuthorityAck(
                AuthorityAckCode.BACKEND_ERROR,
                message='ack lost',
            )

    result = apply_legacy_adoption(
        plan, CommitThenUnknownStore(), now=20)
    assert result.code is OnboardingResultCode.ALREADY_ADOPTED
    assert result.authority.episode_id == LEGACY_A


def test_backend_failure_is_not_reclassified_as_business_quarantine():
    fake, adapter = _adapter()
    key, _flat = _initialize(adapter)
    plan = _legacy_plan(adapter, key)
    fake.raise_eval = True
    result = apply_legacy_adoption(plan, adapter, now=20)
    assert result.code is OnboardingResultCode.BACKEND_ERROR
    assert result.reason is QuarantineReason.BACKEND_UNAVAILABLE


def test_exchange_only_reconstruction_is_planned_and_applied_quarantined():
    _, adapter = _adapter()
    key = _key()
    read = adapter.get_slot_authority(key)
    decision = classify_reconstructed_exposure(
        slot_key=key,
        exchange_side='LONG',
        exchange_quantity='2',
        authority_read=read,
        allow_authority_initialization=True,
    )
    assert decision.classification is \
        AdoptionClassification.RECONSTRUCTED_QUARANTINE
    plan = _reconstruction_plan(
        adapter, key, allow_authority_initialization=True)
    result = apply_reconstructed_episode(plan, adapter, now=20)
    assert result.code is OnboardingResultCode.RECONSTRUCTED_QUARANTINED
    assert result.authority.status is AuthorityStatus.QUARANTINED
    assert result.authority.provenance is AuthorityProvenance.RECONSTRUCTED
    assert result.authority.slot_generation == 1
    assert can_mutate_async(result.authority) is False


def test_reconstruction_reentry_converges_without_generation_increment():
    fake, adapter = _adapter()
    key = _key()
    first_plan = _reconstruction_plan(
        adapter, key, allow_authority_initialization=True)
    first = apply_reconstructed_episode(first_plan, adapter, now=20)
    before = fake.eval_calls
    reentry = _reconstruction_plan(adapter, key, candidate=LEGACY_B)
    result = apply_reconstructed_episode(reentry, adapter, now=30)
    assert reentry.classification is \
        AdoptionClassification.ALREADY_RECONSTRUCTED
    assert result.code is OnboardingResultCode.ALREADY_RECONSTRUCTED
    assert result.authority == first.authority
    assert result.authority.slot_generation == 1
    assert fake.eval_calls == before
    assert result.candidate_discarded is True


def test_stale_fresh_reconstruction_plan_does_not_rebase_high_water():
    _, adapter = _adapter()
    key = _key()
    stale = _reconstruction_plan(
        adapter, key, allow_authority_initialization=True)
    _key_value, flat = _initialize(adapter, key)
    active = adapter.allocate_new_episode(
        key,
        candidate_episode_id=NATIVE_A,
        expected_revision=flat.revision,
        expected_slot_generation=flat.slot_generation,
        expected_episode_id=flat.episode_id,
        now=20,
    ).authority
    closed = adapter.transition_active_to_flat(
        key,
        expected_episode_id=active.episode_id,
        expected_slot_generation=active.slot_generation,
        expected_revision=active.revision,
        now=30,
    ).authority
    result = apply_reconstructed_episode(stale, adapter, now=40)
    assert result.code is OnboardingResultCode.CONFLICT
    assert result.authority == closed
    assert adapter.get_slot_authority(key).authority == closed


def test_two_concurrent_reconstructions_have_one_quarantined_winner():
    _, adapter = _adapter()
    key, _flat = _initialize(adapter)
    plans = [
        _reconstruction_plan(adapter, key, candidate=RECONSTRUCTED_B),
        _reconstruction_plan(adapter, key, candidate=LEGACY_B),
    ]
    barrier = threading.Barrier(3)
    results = []

    def apply(plan):
        barrier.wait()
        results.append(apply_reconstructed_episode(plan, adapter, now=20))

    threads = [threading.Thread(target=apply, args=(plan,)) for plan in plans]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert [result.code for result in results].count(
        OnboardingResultCode.RECONSTRUCTED_QUARANTINED) == 1
    assert [result.code for result in results].count(
        OnboardingResultCode.CONFLICT) == 1
    winner = adapter.get_slot_authority(key).authority
    assert winner.status is AuthorityStatus.QUARANTINED
    assert winner.provenance is AuthorityProvenance.RECONSTRUCTED
    loser = next(result for result in results
                 if result.code is OnboardingResultCode.CONFLICT)
    assert loser.authority == winner


def test_flat_high_water_advances_for_reconstruction_and_stale_plan_loses():
    _, adapter = _adapter()
    key, flat0 = _initialize(adapter)
    stale_legacy = _legacy_plan(adapter, key)
    active_a = adapter.allocate_new_episode(
        key,
        candidate_episode_id=NATIVE_A,
        expected_revision=flat0.revision,
        expected_slot_generation=flat0.slot_generation,
        expected_episode_id=flat0.episode_id,
        now=20,
    ).authority
    flat_a = adapter.transition_active_to_flat(
        key,
        expected_episode_id=active_a.episode_id,
        expected_slot_generation=active_a.slot_generation,
        expected_revision=active_a.revision,
        now=30,
    ).authority
    reconstructed = apply_reconstructed_episode(
        _reconstruction_plan(adapter, key), adapter, now=40)
    stale_result = apply_legacy_adoption(stale_legacy, adapter, now=50)
    assert flat_a.slot_generation == 1
    assert reconstructed.authority.slot_generation == 2
    assert stale_result.code is OnboardingResultCode.CONFLICT
    assert stale_result.authority == reconstructed.authority


def test_active_mutation_predicate_requires_complete_active_authority():
    _, adapter = _adapter()
    key, flat = _initialize(adapter)
    active = adapter.allocate_new_episode(
        key,
        candidate_episode_id=NATIVE_A,
        expected_revision=flat.revision,
        expected_slot_generation=flat.slot_generation,
        expected_episode_id=flat.episode_id,
        now=20,
    ).authority
    closed = active.transition_to_flat(now=30)
    quarantined = closed.allocate_episode(
        episode_id=RECONSTRUCTED_B,
        provenance=AuthorityProvenance.RECONSTRUCTED,
        status=AuthorityStatus.QUARANTINED,
        legacy_position_id_alias=None,
        now=40,
    )
    assert can_mutate_async(active) is True
    assert can_mutate_async(flat) is False
    assert can_mutate_async(closed) is False
    assert can_mutate_async(quarantined) is False
    assert can_mutate_async(None) is False


def test_malformed_authority_and_backend_read_are_distinct():
    fake, adapter = _adapter()
    key = _key()
    fake.data[key.to_storage_key()] = '{broken'
    malformed = _legacy_plan(adapter, key)
    assert malformed.classification is AdoptionClassification.INVALID
    assert malformed.reason is QuarantineReason.MALFORMED_AUTHORITY
    fake.data.clear()
    fake.raise_get = True
    backend = _legacy_plan(adapter, key)
    assert backend.classification is AdoptionClassification.CONFLICT
    assert backend.reason is QuarantineReason.BACKEND_UNAVAILABLE
