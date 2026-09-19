"""Fail-closed runtime activation readiness tests."""

import itertools

import pytest

from position_protection.binance_query import BinanceVerificationEndpoints
from position_protection.verification_activation import (
    VerificationActivationBlocker,
    VerificationActivationCode,
    VerificationActivationManifest,
    assess_verification_activation,
)


def _endpoints():
    return BinanceVerificationEndpoints("/approved/position", "/approved/algo")


def test_default_manifest_is_disabled_and_does_not_claim_missing_dependencies():
    decision = assess_verification_activation(VerificationActivationManifest())
    assert decision.code is VerificationActivationCode.DISABLED
    assert decision.blockers == (VerificationActivationBlocker.FEATURE_DISABLED,)
    assert not decision.ready


def test_enabled_manifest_reports_every_missing_contract_in_stable_order():
    decision = assess_verification_activation(
        VerificationActivationManifest(enabled=True)
    )
    assert decision.code is VerificationActivationCode.BLOCKED
    assert decision.blockers == (
        VerificationActivationBlocker.ENDPOINTS_UNAPPROVED,
        VerificationActivationBlocker.DURABLE_SCHEDULER_UNAPPROVED,
        VerificationActivationBlocker.OPERATOR_OUTCOME_SINK_UNAPPROVED,
    )


@pytest.mark.parametrize(
    ("endpoint_ready", "scheduler_ready", "operator_ready"),
    tuple(itertools.product((False, True), repeat=3)),
)
def test_ready_requires_all_three_independent_approvals(
    endpoint_ready, scheduler_ready, operator_ready
):
    decision = assess_verification_activation(
        VerificationActivationManifest(
            enabled=True,
            endpoints=_endpoints() if endpoint_ready else None,
            endpoint_contract_ref="D2C-ENDPOINT-v1" if endpoint_ready else "",
            durable_scheduler_contract_ref="D3-SCHEDULER-v1"
            if scheduler_ready
            else "",
            operator_runbook_ref="RUNBOOK-D2C-v1" if operator_ready else "",
        )
    )
    assert decision.ready is (endpoint_ready and scheduler_ready and operator_ready)
    assert (decision.code is VerificationActivationCode.READY) is decision.ready


def test_endpoint_path_without_approval_reference_remains_blocked():
    decision = assess_verification_activation(
        VerificationActivationManifest(
            enabled=True,
            endpoints=_endpoints(),
            durable_scheduler_contract_ref="scheduler",
            operator_runbook_ref="runbook",
        )
    )
    assert decision.blockers == (
        VerificationActivationBlocker.ENDPOINTS_UNAPPROVED,
    )


@pytest.mark.parametrize("enabled", [1, "true", None])
def test_enabled_requires_an_actual_boolean(enabled):
    with pytest.raises(TypeError):
        VerificationActivationManifest(enabled=enabled)


@pytest.mark.parametrize("invalid", ["bad\nref", "ref\n", "\tref"])
def test_references_are_trimmed_but_control_characters_are_rejected(invalid):
    manifest = VerificationActivationManifest(endpoint_contract_ref="  ref  ")
    assert manifest.endpoint_contract_ref == "ref"
    with pytest.raises(ValueError):
        VerificationActivationManifest(operator_runbook_ref=invalid)


def test_wrong_manifest_or_endpoint_type_is_rejected():
    with pytest.raises(TypeError):
        assess_verification_activation({})
    with pytest.raises(TypeError):
        VerificationActivationManifest(endpoints="/path")
