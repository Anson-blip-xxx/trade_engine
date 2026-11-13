"""Research projections reuse exact audit evidence, never grant order authority."""

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from test_v2_directional_exit_stage import SCOPE, setup_exit

import v2_core.account_coverage as coverage_module
from tests.support.v2_admission import database as database_fixture
from v2_core.account_coverage import AccountCoverageAudit

database = database_fixture


def observation():
    row = {
        "symbol": "BTCUSDT",
        "positionSide": "BOTH",
        "positionAmt": "1.25",
        "updateTime": 999_999_999,
        "unrelated_field": "must-not-be-projected",
    }
    return {
        "account_scope": asdict(SCOPE),
        "started_at_ms": 1000,
        "finished_at_ms": 1200,
        "failures": {},
        "responses": {
            "positions_before": [deepcopy(row)],
            "positions_after": [deepcopy(row)],
        },
    }


def bare_audit():
    audit = object.__new__(AccountCoverageAudit)
    audit.scope = SCOPE
    audit.excluded_position_symbols = ()
    audit._research_positions = {}
    return audit


def test_projection_is_detached_scoped_and_preserves_actual_query_interval():
    audit, raw = bare_audit(), observation()
    audit._remember_research_positions(raw)
    expected = {
        "account_scope": asdict(SCOPE),
        "response": {
            "symbol": "BTCUSDT",
            "positionSide": "BOTH",
            "positionAmt": "1.25",
        },
        "observed_at_ms": 1000,
        "available_at_ms": 1200,
    }
    proof = audit.research_position("BTCUSDT")
    assert proof == expected
    raw["responses"]["positions_after"][0]["positionAmt"] = "77"
    proof["response"]["positionAmt"] = "88"
    assert audit.research_position("BTCUSDT") == expected
    assert audit.research_position("ETHUSDT") is None
    assert audit.research_position(None) is None
    audit.scope = replace(SCOPE, account_id="different-qa-account")
    assert audit.research_position("BTCUSDT") is None


@pytest.mark.parametrize(
    "defect",
    [
        "scope",
        "negative_time",
        "boolean_time",
        "future_start",
        "unsafe_time",
        "failure",
        "changing_quantity",
        "changing_side",
        "duplicate",
        "hedge",
        "invalid_amount",
        "missing_position",
        "wrong_type",
    ],
)
def test_invalid_new_projection_clears_prior_evidence_and_is_failsoft(defect):
    audit = bare_audit()
    audit._remember_research_positions(observation())
    assert audit.research_position("BTCUSDT") is not None
    raw = observation()
    if defect == "scope":
        raw["account_scope"]["environment"] = "LIVE"
    elif defect == "negative_time":
        raw["started_at_ms"] = -1
    elif defect == "boolean_time":
        raw["started_at_ms"] = True
    elif defect == "future_start":
        raw["started_at_ms"] = 1201
    elif defect == "unsafe_time":
        raw["finished_at_ms"] = 2**53
    elif defect == "failure":
        raw["failures"] = {"positions_after": "ACCOUNT_READ_UNAVAILABLE"}
    elif defect == "changing_quantity":
        raw["responses"]["positions_after"][0]["positionAmt"] = "2"
    elif defect == "changing_side":
        raw["responses"]["positions_after"][0]["positionAmt"] = "-1.25"
    elif defect == "duplicate":
        raw["responses"]["positions_after"] *= 2
    elif defect == "hedge":
        for rows in raw["responses"].values():
            rows[0]["positionSide"] = "LONG"
    elif defect == "invalid_amount":
        raw["responses"]["positions_after"][0]["positionAmt"] = "nan"
    elif defect == "missing_position":
        raw["responses"].pop("positions_after")
    else:
        raw = object()
    audit._remember_research_positions(raw)
    assert audit.research_position("BTCUSDT") is None


@pytest.mark.parametrize("quantity", ["0", "-1.25"])
def test_zero_is_not_protection_evidence_and_short_sign_is_preserved(quantity):
    audit, raw = bare_audit(), observation()
    for rows in raw["responses"].values():
        rows[0]["positionAmt"] = quantity
    audit._remember_research_positions(raw)
    proof = audit.research_position("BTCUSDT")
    if quantity == "0":
        assert proof is None
    else:
        assert proof["response"]["positionAmt"] == quantity
    audit.excluded_position_symbols = ("BTCUSDT",)
    audit._remember_research_positions(raw)
    assert audit.research_position("BTCUSDT") is None


def test_clear_audit_exposes_projection_without_expanding_result_or_queries(database):
    _, _, venue, service = setup_exit(database)
    venue.calls.clear()
    result = service.audit.run_once()
    assert result["status"] == "ACCOUNT_COVERAGE_CLEAR"
    assert set(result) == {
        "status",
        "blockers",
        "excluded_position_symbols",
        "execution_authorized",
        "observation_id",
    }
    assert len(venue.calls) == 7
    proof = service.audit.research_position("BTCUSDT")
    assert proof["observed_at_ms"] == proof["available_at_ms"] == service.clock()
    assert proof["response"]["positionAmt"] == "1.25"
    assert len(venue.calls) == 7 and not venue.order_writes


def test_cross_instance_reuse_projects_persisted_inventory_without_new_io(database):
    _, _, venue, service = setup_exit(database)
    source = service.audit.run_once()
    other = AccountCoverageAudit(database, venue, scope=SCOPE, clock_ms=service.clock)
    venue.calls.clear()
    assert other.research_position("BTCUSDT") is None
    assert other.reuse_after_protection(source) == source
    assert other.research_position("BTCUSDT") == service.audit.research_position(
        "BTCUSDT"
    )
    assert not venue.calls and not venue.order_writes
    # The reader itself is pure: after reuse even unavailable DB/clock cannot
    # trigger I/O or manufacture a newer acquisition time.
    other.connect = lambda: pytest.fail("research opened database")
    other.inventory.clock_ms = lambda: pytest.fail("research refreshed clock")
    assert other.research_position("BTCUSDT")["available_at_ms"] == service.clock()


@pytest.mark.parametrize("defect", ["missing", "expired", "scope", "tampered"])
def test_reuse_failure_drops_previous_projection(database, defect):
    _, _, venue, service = setup_exit(database)
    proof = service.audit.run_once()
    assert service.audit.research_position("BTCUSDT")
    if defect == "missing":
        proof = None
    elif defect == "expired":
        now = service.clock()
        service.audit.inventory.clock_ms = lambda: now + 5001
    elif defect == "scope":
        service.audit.scope = replace(SCOPE, account_id="another-qa-account")
    else:
        proof = {**proof, "execution_authorized": True}
    venue.calls.clear()
    assert service.audit.reuse_after_protection(proof) is None
    assert service.audit.research_position("BTCUSDT") is None
    assert not venue.calls and not venue.order_writes


@pytest.mark.parametrize("defect", ["blocked", "busy", "exception"])
def test_failed_fresh_audit_cannot_leave_previous_projection(
    database, monkeypatch, defect
):
    _, _, venue, service = setup_exit(database)
    service.audit.run_once()
    assert service.audit.research_position("BTCUSDT")
    if defect == "blocked":
        venue.rows["/fapi/v3/positionRisk"][0]["positionAmt"] = "2"
        assert service.audit.run_once()["status"] == "ACCOUNT_COVERAGE_BLOCKED"
    elif defect == "busy":
        monkeypatch.setattr(
            coverage_module, "session_advisory_lock", lambda *_: nullcontext(False)
        )
        assert service.audit.run_once()["status"] == "BUSY"
    else:

        def fail():
            raise RuntimeError("isolated QA failure")

        monkeypatch.setattr(service.audit, "facts", fail)
        with pytest.raises(RuntimeError, match="isolated QA failure"):
            service.audit.run_once()
    assert service.audit.research_position("BTCUSDT") is None
    assert not venue.order_writes


@pytest.mark.parametrize("mode", ["reuse", "fresh"])
def test_failed_lock_handoff_cannot_publish_research_projection(
    database, monkeypatch, mode
):
    _, _, _, service = setup_exit(database)
    proof = service.audit.run_once()
    assert service.audit.research_position("BTCUSDT")

    @contextmanager
    def fail_handoff(*_):
        yield True
        raise RuntimeError("isolated QA lock handoff failure")

    monkeypatch.setattr(coverage_module, "session_advisory_lock", fail_handoff)
    if mode == "reuse":
        assert service.audit.reuse_after_protection(proof) is None
    else:
        with pytest.raises(RuntimeError, match="isolated QA lock handoff failure"):
            service.audit.run_once()
    assert service.audit.research_position("BTCUSDT") is None
