"""Strict pure verification of current-generation exchange protection."""

from dataclasses import replace
from uuid import uuid4

import pytest

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
    verify_current_protection,
)


def _records():
    key = ExchangePositionKey.one_way(
        account_principal_id="verification",
        environment="SANDBOX",
        symbol="BTCUSDT",
    )
    episode = str(uuid4())
    authority = SlotAuthority.initial_flat(key, now=1).allocate_episode(
        episode_id=episode,
        provenance=AuthorityProvenance.MIGRATED,
        status=AuthorityStatus.ACTIVE,
        legacy_position_id_alias="legacy:1",
        now=2,
    )
    projection = LivePositionProjection(
        exchange_position_key=key,
        episode_id=episode,
        slot_generation=authority.slot_generation,
        identity_provenance=AuthorityProvenance.MIGRATED,
        state_revision=1,
        last_operation_id="project:1",
        side="LONG",
        system="S6",
        quantity=2,
        entry_price=100,
        opened_at=1,
        updated_at=2,
        legacy_position_id_alias="legacy:1",
    )
    desired = DesiredProtectionRecord.initial_pending(
        exchange_position_key=key,
        episode_id=episode,
        slot_generation=authority.slot_generation,
        desired_intent_id="protect:1",
        trigger_price=90,
        covered_quantity=2,
        closing_side="SELL",
        operation_id="declare:1",
        now=2,
    ).transition(
        status=ProtectionStatus.SUBMITTING,
        operation_id="submit:1",
        now=3,
    )
    desired = desired.transition(
        status=ProtectionStatus.SUBMITTING,
        operation_id="alias:1",
        now=4,
        exchange_algo_aliases=("77",),
        allow_same_status_alias_repair=True,
    )
    exposure = ExchangeExposureObservation(key, "LONG", 2, 5)
    order = ExchangeProtectionObservation(
        key,
        "77",
        "WORKING",
        "STOP_MARKET",
        True,
        "SELL",
        90,
        2,
        5,
    )
    return authority, projection, desired, exposure, order


def _verify(*, mutate=None, orders=None, now=6, max_age=5):
    authority, projection, desired, exposure, order = _records()
    values = {
        "authority": authority,
        "projection": projection,
        "desired": desired,
        "exposure": exposure,
        "orders": (order,) if orders is None else orders(order),
        "now": now,
        "max_evidence_age": max_age,
        "quantity_tolerance": 0.01,
        "trigger_tolerance": 0.01,
    }
    if mutate is not None:
        mutate(values)
    return verify_current_protection(**values)


def test_exact_current_generation_exchange_evidence_is_verified():
    result = _verify()
    assert result.code is ProtectionVerificationCode.VERIFIED
    assert result.verified
    assert result.order.algo_alias == "77"


def test_exchange_ack_alias_without_active_status_is_not_verified():
    result = _verify(orders=lambda order: (replace(order, status="FINISHED"),))
    assert result.code is ProtectionVerificationCode.ORDER_NOT_ACTIVE
    assert not result.verified


@pytest.mark.parametrize("field", ["episode", "generation", "slot"])
def test_canonical_identity_mismatch_fails_closed(field):
    def mutate(values):
        desired = values["desired"]
        if field == "episode":
            values["desired"] = replace(desired, episode_id=str(uuid4()))
        elif field == "generation":
            values["desired"] = replace(
                desired, slot_generation=desired.slot_generation + 1
            )
        else:
            other = ExchangePositionKey.one_way(
                account_principal_id="verification",
                environment="SANDBOX",
                symbol="ETHUSDT",
            )
            values["desired"] = replace(desired, exchange_position_key=other)

    assert _verify(mutate=mutate).code is ProtectionVerificationCode.IDENTITY_MISMATCH


def test_flat_authority_cannot_be_verified():
    def mutate(values):
        values["authority"] = values["authority"].transition_to_flat(now=6)

    assert _verify(mutate=mutate).code is ProtectionVerificationCode.AUTHORITY_INACTIVE


def test_terminal_desired_state_cannot_be_verified():
    def mutate(values):
        values["desired"] = values["desired"].transition(
            status=ProtectionStatus.CANCELLED,
            operation_id="cancel:1",
            now=5,
        )

    assert _verify(mutate=mutate).code is ProtectionVerificationCode.DESIRED_NOT_VERIFYABLE


@pytest.mark.parametrize("target", ["exposure", "order"])
def test_stale_or_pre_desired_evidence_is_rejected(target):
    def mutate(values):
        if target == "order":
            values["orders"] = (replace(values["orders"][0], observed_at=3),)
        else:
            values["exposure"] = replace(values["exposure"], observed_at=3)

    assert _verify(mutate=mutate).code is ProtectionVerificationCode.STALE_EVIDENCE


@pytest.mark.parametrize(
    ("field", "value"),
    [("side", "SHORT"), ("quantity", 3)],
)
def test_exchange_exposure_must_match_projection_and_coverage(field, value):
    def mutate(values):
        values["exposure"] = replace(values["exposure"], **{field: value})

    assert _verify(mutate=mutate).code is ProtectionVerificationCode.EXPOSURE_MISMATCH


def test_missing_alias_never_adopts_order_by_symbol_only():
    def mutate(values):
        values["desired"] = replace(values["desired"], exchange_algo_aliases=())

    assert _verify(mutate=mutate).code is ProtectionVerificationCode.NO_ALIAS


def test_unobserved_alias_is_not_verified():
    assert _verify(orders=lambda order: ()).code is ProtectionVerificationCode.ORDER_NOT_FOUND


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("order_type", "TAKE_PROFIT_MARKET"),
        ("reduce_only", False),
        ("closing_side", "BUY"),
        ("trigger_price", 91),
        ("quantity", 3),
    ],
)
def test_every_exchange_order_spec_field_is_strict(field, value):
    result = _verify(orders=lambda order: (replace(order, **{field: value}),))
    assert result.code is ProtectionVerificationCode.ORDER_SPEC_MISMATCH


def test_triggered_order_is_not_claimed_as_still_active():
    result = _verify(orders=lambda order: (replace(order, status="TRIGGERED"),))
    assert result.code is ProtectionVerificationCode.ORDER_NOT_ACTIVE


def test_multiple_current_alias_orders_are_ambiguous():
    def mutate(values):
        values["desired"] = replace(
            values["desired"], exchange_algo_aliases=("77", "78")
        )
        first = values["orders"][0]
        values["orders"] = (first, replace(first, algo_alias="78"))

    assert _verify(mutate=mutate).code is ProtectionVerificationCode.AMBIGUOUS_ACTIVE_ORDERS


def test_foreign_active_stop_for_same_slot_is_ambiguous():
    result = _verify(
        orders=lambda order: (order, replace(order, algo_alias="foreign"))
    )
    assert result.code is ProtectionVerificationCode.AMBIGUOUS_ACTIVE_ORDERS


def test_terminal_alias_from_same_generation_does_not_make_active_order_ambiguous():
    def mutate(values):
        values["desired"] = replace(
            values["desired"], exchange_algo_aliases=("old", "77")
        )
        current = values["orders"][0]
        values["orders"] = (
            replace(current, algo_alias="old", status="FINISHED"),
            current,
        )

    assert _verify(mutate=mutate).code is ProtectionVerificationCode.VERIFIED


def test_terminal_foreign_order_does_not_block_current_verification():
    result = _verify(
        orders=lambda order: (
            order,
            replace(order, algo_alias="foreign", status="FINISHED"),
        )
    )
    assert result.code is ProtectionVerificationCode.VERIFIED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_evidence_age", -1),
        ("quantity_tolerance", -1),
        ("trigger_tolerance", -1),
    ],
)
def test_negative_verification_bounds_are_invalid(field, value):
    authority, projection, desired, exposure, order = _records()
    kwargs = {
        "authority": authority,
        "projection": projection,
        "desired": desired,
        "exposure": exposure,
        "orders": (order,),
        "now": 6,
        "max_evidence_age": 5,
        "quantity_tolerance": 0,
        "trigger_tolerance": 0,
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match="nonnegative"):
        verify_current_protection(**kwargs)
