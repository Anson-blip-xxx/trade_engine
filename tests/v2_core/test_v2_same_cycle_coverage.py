"""Real PG evidence + fake venue: diagnostic reuse never authorizes a POST."""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from test_v2_directional_exit_stage import SCOPE, setup_exit
from test_v2_trading_pipeline import case as pipeline_fixture

from tests.support.v2_admission import database as database_fixture
from v2_core.account_coverage import AccountCoverageAudit
from v2_core.state import BusinessState, StateKey

database = database_fixture
case = pipeline_fixture


def evidence_key(identity, scope=SCOPE):
    return StateKey(
        **asdict(scope), namespace="account-coverage-evidence-v1", key=identity
    )


def copy_proof(database, proof, mutate, *, reason="ACCOUNT_COVERAGE_AUDIT"):
    """Create explicitly corrupted isolated-QA evidence, never edit history."""
    saved = BusinessState(database).read(evidence_key(proof["observation_id"]))
    payload = json.loads(saved.payload_json)
    identity = str(uuid4())
    payload["inventory"]["observation_id"] = identity
    mutate(payload)
    BusinessState(database).change(
        evidence_key(identity),
        expected_version=0,
        request_key=identity,
        payload=payload,
        reason=reason,
    )
    return {**payload["result"], "observation_id": identity}


@pytest.mark.parametrize("combined", [False, True])
def test_same_cycle_reuses_persisted_diagnostic_without_refreshing_timestamp(
    database, combined
):
    _, _, venue, service = setup_exit(database)
    if combined:
        venue.rows["/fapi/v1/accountConfig"] = {
            "dualSidePosition": False,
            "multiAssetsMargin": False,
        }
        BusinessState(database).change(
            StateKey(
                **asdict(SCOPE), namespace="account-inventory-policy-v1", key="current"
            ),
            expected_version=0,
            request_key="qa-enable",
            reason="QA",
            payload={"combined_account_config": True},
        )
    proof = service.audit.run_once()
    original = BusinessState(database).read(evidence_key(proof["observation_id"]))
    venue.calls.clear()
    first = service.run_after_protection(proof)
    assert first["status"] == "CLEAR", first
    assert first["coverage_reused"] is True
    assert first["coverage"] == proof
    assert first["execution_authorized"] is False
    assert not venue.calls and not venue.order_writes
    assert (
        BusinessState(database).read(evidence_key(proof["observation_id"])) == original
    )
    # No cross-cycle cache: the standalone API still takes a full inventory.
    second = service.run_once()
    assert second["coverage_reused"] is False
    assert second["coverage"]["observation_id"] != proof["observation_id"]
    assert len(venue.calls) == (6 if combined else 7)


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "nonpersistent",
        "caller_tamper",
        "wrong_scope",
        "exclusions",
        "expired",
        "future",
        "slow_acquisition",
        "noninteger_time",
        "blocked",
        "failures",
        "missing_response",
        "bad_digest",
        "changing_facts",
        "wrong_reason",
    ],
)
def test_untrusted_stale_or_incomplete_proof_falls_back_to_full_audit(database, defect):
    _, _, venue, service = setup_exit(database)
    proof = service.audit.run_once()
    now = service.clock()
    if defect == "missing":
        proof = None
    elif defect == "nonpersistent":
        proof = {**proof, "observation_id": str(uuid4())}
    elif defect == "caller_tamper":
        proof = {**proof, "execution_authorized": True}
    elif defect == "wrong_scope":
        proof = copy_proof(
            database,
            proof,
            lambda payload: payload["inventory"]["account_scope"].update(
                account_id="other-account"
            ),
        )
    elif defect == "exclusions":
        service.audit.excluded_position_symbols = ("ETHUSDT",)
    elif defect in {"expired", "future"}:
        service.audit.inventory.clock_ms = lambda: (
            now + (5001 if defect == "expired" else -1)
        )
    else:

        def mutate(payload):
            inventory = payload["inventory"]
            if defect == "slow_acquisition":
                inventory["started_at_ms"] = now - 5001
            elif defect == "noninteger_time":
                inventory["started_at_ms"] = str(now)
            elif defect == "blocked":
                payload["result"]["blockers"] = ["INCOMPLETE_ACCOUNT_READ"]
            elif defect == "failures":
                inventory["failures"] = {"account": "FAILED"}
            elif defect == "missing_response":
                inventory["responses"].pop("account")
            elif defect == "bad_digest":
                inventory["response_digests"]["account"] = "0" * 64
            elif defect == "changing_facts":
                payload["before"]["episodes"][0]["revision"] += 1

        proof = copy_proof(
            database,
            proof,
            mutate,
            reason="NOT_AN_AUDIT"
            if defect == "wrong_reason"
            else "ACCOUNT_COVERAGE_AUDIT",
        )
    venue.calls.clear()
    answer = service.run_after_protection(proof)
    # A rejected proof must never shortcut the existing full-audit fallback.
    assert len(venue.calls) == 7, answer
    assert answer.get("coverage_reused") is not True
    assert not venue.order_writes


def test_local_ledger_change_invalidates_proof_even_with_same_exposure(database):
    runtime, opened, venue, service = setup_exit(database)
    proof = service.audit.run_once()
    before = service.audit.facts()
    runtime.data.ledger.adjustment(
        episode_id=opened["intent_id"],
        source_id="qa-funding",
        amount_text="-0.1",
        currency="USDT",
        kind="FUNDING",
        occurred_at_ms=600003,
        evidence={"source": "QA"},
    )
    assert service.audit.facts() != before
    venue.calls.clear()
    answer = service.run_after_protection(proof)
    assert answer["coverage_reused"] is False
    assert len(venue.calls) == 7


@pytest.mark.parametrize("outcome", ["RECONCILED", "ACKNOWLEDGED", "POISON"])
def test_close_recovery_or_poison_never_reuses_pre_recovery_proof(
    database, monkeypatch, outcome
):
    runtime, _, venue, service = setup_exit(database)
    proof = service.audit.run_once()
    trace = service._trace

    def pending(episode):
        result = trace(episode)
        if outcome == "POISON":
            raise ValueError("QA poison")
        return (*result[:-1], [{"order_id": str(uuid4())}])

    monkeypatch.setattr(service, "_trace", pending)
    monkeypatch.setattr(runtime.execution, "recover", lambda _: outcome)
    monkeypatch.setattr(
        service.audit,
        "reuse_after_protection",
        lambda _: pytest.fail("reuse after recovery"),
    )
    venue.calls.clear()
    result = service.run_after_protection(proof)
    if outcome != "RECONCILED":
        assert result["reason"] == "CLOSE_RECOVERY_PENDING"
    else:
        assert len(venue.calls) == 7
    assert not venue.order_writes


@pytest.mark.parametrize("venue_changed", [False, True])
def test_actual_reduce_only_dispatch_still_requires_new_full_inventory(
    database, venue_changed
):
    _, opened, venue, service = setup_exit(
        database, allow_writes=True, mark_price="105.1"
    )
    proof = service.audit.run_once()
    if venue_changed:
        venue.rows["/fapi/v3/positionRisk"][0]["positionAmt"] = "2"
    venue.calls.clear()
    answer = service.run_after_protection(proof)
    assert answer["coverage_reused"] is True
    assert len(venue.calls) >= 7, answer
    if venue_changed:
        assert not venue.order_writes
        assert answer["exits"][opened["intent_id"]]["status"] == "BLOCKED"
    else:
        assert len(venue.order_writes) == 1
        assert venue.order_writes[0]["reduceOnly"] == "true"
        service.run_after_protection(proof)
        assert len(venue.order_writes) == 1


def test_emergency_reduction_never_uses_diagnostic_reuse_path(database, monkeypatch):
    _, opened, venue, service = setup_exit(database, allow_writes=True)
    monkeypatch.setattr(
        service.audit,
        "reuse_after_protection",
        lambda _: pytest.fail("emergency reused diagnostic"),
    )
    venue.calls.clear()
    service._unprotected_exit(opened["intent_id"])
    assert len(venue.calls) >= 7
    assert not venue.order_writes  # Existing native stop remains in force.


def test_pipeline_passes_only_current_clear_protection_proof(case):
    pipeline, _, _, _, _ = case
    proof = {"observation_id": str(uuid4()), "status": "ACCOUNT_COVERAGE_CLEAR"}
    supplied = []
    pipeline.protection.run_once = lambda: {
        "status": "CLEAR",
        "coverage": deepcopy(proof),
    }

    def after_protection(coverage):
        supplied.append(coverage)
        return {"status": "CLEAR"}

    pipeline.exits.run_after_protection = after_protection
    pipeline.exits.run_once = lambda: pytest.fail("dedicated same-cycle hook missing")
    pipeline.run_once()
    assert supplied == [proof]
    pipeline.protection.run_once = lambda: {"status": "BLOCKED", "coverage": proof}
    pipeline.run_once()
    assert supplied == [proof, None]


def test_reuse_source_database_failure_is_only_a_miss(database, monkeypatch):
    _, _, _, service = setup_exit(database)
    proof = service.audit.run_once()
    monkeypatch.setattr(
        BusinessState,
        "read",
        lambda *_: (_ for _ in ()).throw(RuntimeError("QA DB failure")),
    )
    assert service.audit.reuse_after_protection(proof) is None


def test_wrong_account_cannot_read_another_accounts_proof(database):
    _, _, venue, service = setup_exit(database)
    proof = service.audit.run_once()
    venue = deepcopy(venue)
    venue.account_id = "other-account"
    other = AccountCoverageAudit(
        database,
        venue,
        scope=replace(SCOPE, account_id="other-account"),
        clock_ms=service.clock,
    )
    assert other.reuse_after_protection(proof) is None


@pytest.mark.parametrize(
    "payload,deleted",
    [
        ({"combined_account_config": "true"}, False),
        ({"combined_account_config": 1}, False),
        ({"combined_account_config": True, "unexpected": True}, False),
        ({}, True),
    ],
)
def test_invalid_or_deleted_inventory_policy_fails_closed_before_venue_read(
    database, payload, deleted
):
    _, _, venue, service = setup_exit(database)
    BusinessState(database).change(
        StateKey(
            **asdict(SCOPE), namespace="account-inventory-policy-v1", key="current"
        ),
        expected_version=0,
        request_key="qa-policy",
        reason="QA",
        payload=payload,
        deleted=deleted,
    )
    venue.calls.clear()
    with pytest.raises(ValueError, match="INVALID_ACCOUNT_INVENTORY_POLICY"):
        service.audit.run_once()
    assert not venue.calls


def test_independent_inventory_policy_is_scope_isolated_and_old_runtime_policy_compatible(
    database,
):
    from v2_core.runtime_policy import PolicyStore

    _, _, venue, service = setup_exit(database)
    before = PolicyStore(database, SCOPE).read()
    assert service.audit._combined_account_config() is False
    BusinessState(database).change(
        StateKey(
            **asdict(SCOPE), namespace="account-inventory-policy-v1", key="current"
        ),
        expected_version=0,
        request_key="qa-policy",
        reason="QA",
        payload={"combined_account_config": True},
    )
    assert service.audit._combined_account_config() is True
    assert PolicyStore(database, SCOPE).read() == before
    other_reader = deepcopy(venue)
    other_reader.account_id = "other-account"
    other = AccountCoverageAudit(
        database,
        other_reader,
        scope=replace(SCOPE, account_id="other-account"),
        clock_ms=service.clock,
    )
    assert other._combined_account_config() is False


@pytest.mark.parametrize(
    "defect", ["source_digest", "missing_derived", "original_mode"]
)
def test_combined_inventory_requires_real_source_binding(database, defect):
    _, _, venue, service = setup_exit(database)
    venue.rows["/fapi/v1/accountConfig"] = {
        "dualSidePosition": False,
        "multiAssetsMargin": False,
    }
    service.audit.inventory.combined_account_config = True
    proof = service.audit.run_once()

    def mutate(payload):
        inventory = payload["inventory"]
        if defect == "source_digest":
            inventory["derived_responses"]["mode"]["source_digest"] = "0" * 64
        elif defect == "missing_derived":
            inventory.pop("derived_responses")
        else:
            inventory["responses"]["account_config"]["dualSidePosition"] = True

    proof = copy_proof(database, proof, mutate)
    venue.calls.clear()
    result = service.run_after_protection(proof)
    assert result["coverage_reused"] is False
    assert len(venue.calls) == 6


def test_five_second_boundary_uses_started_time_not_a_refreshed_deadline(database):
    _, _, _, service = setup_exit(database)
    proof = service.audit.run_once()
    original = service.clock()
    service.audit.inventory.clock_ms = lambda: original + 5000
    assert service.audit.reuse_after_protection(proof) == proof
    service.audit.inventory.clock_ms = lambda: original + 5001
    assert service.audit.reuse_after_protection(proof) is None
