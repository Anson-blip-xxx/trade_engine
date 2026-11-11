"""Aged same-cycle coverage must not turn income lag into a global entry stop."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_v2_directional_cash import closed as closed_fixture
from test_v2_settlement_entry import setup

from services import v2_settlement_entry as module
from tests.support.v2_admission import database as database_fixture
from v2_core.state import BusinessState

database = database_fixture
closed = closed_fixture


def test_expired_valid_witness_refreshes_once_without_settling_or_releasing(
    database, closed
):
    runtime, episode, venue, stage, result, proof = setup(database, closed)
    original = deepcopy(proof)
    before = runtime.data.trace(episode)
    # Production pattern: protection then exits consumes > 5 seconds while the
    # known exchange income publication lag is still < 60 seconds.
    stage.clock = stage.coverage.inventory.clock_ms = lambda: 10000
    assert stage.coverage.reuse_after_protection(proof) is None
    assert stage.coverage.expired_after_protection(proof) is True
    answer = stage.entry_clearance(result, proof)
    assert answer is not None
    assert answer["status"] == "ENTRY_CLEAR_PENDING_INCOME"
    assert answer["evidence"]["coverage_observation_id"] != proof["observation_id"]
    assert answer["evidence"]["coverage_refresh"] == {
        "reason": "PERSISTED_COVERAGE_AGE_EXPIRED",
        "previous_observation_id": proof["observation_id"],
        "attempts": 1,
    }
    assert answer["execution_authorized"] is answer["settlement_authorized"] is False
    assert answer["risk_reservations_retained"] is True
    assert proof == original and runtime.data.trace(episode) == before
    # One complete inventory, not looping until a convenient observation.
    assert len(venue.calls) == 7
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_settlements").fetchone() == (0,)
        assert c.execute(
            "SELECT status,released_at FROM v2_risk_reservations WHERE episode_id=%s",
            (episode,),
        ).fetchone() == ("HELD", None)


@pytest.mark.parametrize(
    "defect",
    ["missing", "tampered", "changed_facts", "nonlag", "released", "expired_lag"],
)
def test_unknown_or_unsafe_case_never_starts_refresh(
    database, closed, monkeypatch, defect
):
    runtime, episode, _, stage, result, proof = setup(database, closed)
    stage.clock = stage.coverage.inventory.clock_ms = lambda: 10000
    if defect == "missing":
        proof = None
    elif defect == "tampered":
        proof = {**proof, "execution_authorized": True}
    elif defect == "changed_facts":
        runtime.data.ledger.adjustment(
            episode_id=episode,
            source_id="qa-later",
            amount_text="-0.1",
            currency="USDT",
            kind="FUNDING",
            occurred_at_ms=11,
            evidence={"source": "qa"},
        )
    elif defect == "nonlag":
        result["settlements"][episode]["reason"] = "CASH_MISMATCH"
    elif defect == "released":
        with database() as c:
            c.execute(
                "UPDATE v2_risk_reservations SET status='RELEASED',released_at=clock_timestamp(),release_reason='ABORTED' WHERE episode_id=%s",
                (episode,),
            )
    else:
        stage.clock = stage.coverage.inventory.clock_ms = lambda: 70000
    monkeypatch.setattr(
        module, "_refresh_coverage", lambda *_: pytest.fail("unsafe refresh")
    )
    assert stage.entry_clearance(result, proof) is None


@pytest.mark.parametrize(
    "outcome", ["blocked", "late", "policy_changed", "facts_changed"]
)
def test_refresh_is_one_attempt_and_rechecks_all_fences(
    database, closed, monkeypatch, outcome
):
    runtime, episode, _, stage, result, proof = setup(database, closed)
    stage.clock = stage.coverage.inventory.clock_ms = lambda: 10000
    actual, calls = module._refresh_coverage, []

    def refresh(target):
        calls.append(True)
        fresh = actual(target)
        if outcome == "blocked":
            fresh["status"] = "ACCOUNT_COVERAGE_BLOCKED"
        elif outcome == "late":
            stage.clock = stage.coverage.inventory.clock_ms = lambda: 16000
        elif outcome == "policy_changed":
            BusinessState(database).change(
                module.policy_key(stage.scope),
                expected_version=1,
                request_key="qa-off-refresh",
                reason="QA",
                payload={"enabled": False},
            )
        else:
            runtime.data.ledger.adjustment(
                episode_id=episode,
                source_id="qa-refresh-change",
                amount_text="-0.1",
                currency="USDT",
                kind="FUNDING",
                occurred_at_ms=11,
                evidence={"source": "qa"},
            )
        return fresh

    monkeypatch.setattr(module, "_refresh_coverage", refresh)
    assert stage.entry_clearance(result, proof) is None
    assert calls == [True]


@pytest.mark.parametrize(
    "defect",
    ["none", "late_get", "second_get", "write", "wrong_route", "params", "sql_failure"],
)
def test_refresh_budget_and_read_only_contract_without_database(monkeypatch, defect):
    clock, calls, sql = [0.0], [], []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def request(method, path, params):
        calls.append((method, path, params))
        if defect in {"late_get", "second_get"}:
            clock[0] = 5.1
        return {}

    request.account_id, request.environment = "qa", "SANDBOX"

    class Connection:
        def execute(self, statement):
            sql.append(statement)
            if defect == "sql_failure":
                raise TimeoutError("qa")

    @contextmanager
    def connect():
        yield Connection()

    class Audit:
        def __init__(self, bounded_connect, bounded_request, **kwargs):
            self.connect, self.request = bounded_connect, bounded_request
            self.inventory = SimpleNamespace(max_duration_ms=30000)

        def run_once(self):
            assert self.inventory.max_duration_ms == 5000
            with self.connect():
                pass
            route, method, params = "/fapi/v3/account", "GET", {}
            if defect == "write":
                method = "POST"
            if defect == "wrong_route":
                route = "/fapi/v1/order"
            if defect == "params":
                params = {"symbol": "BTCUSDT"}
            self.request(method, route, params)
            if defect == "second_get":
                self.request("GET", "/fapi/v3/account", {})
            return {"status": "ACCOUNT_COVERAGE_CLEAR"}

    monkeypatch.setattr(module, "AccountCoverageAudit", Audit)
    stage = SimpleNamespace(
        connect=connect,
        scope="qa",
        clock=lambda: 1,
        coverage=SimpleNamespace(
            inventory=SimpleNamespace(request=request), excluded_position_symbols=()
        ),
    )
    if defect == "none":
        assert module._refresh_coverage(stage)["status"] == "ACCOUNT_COVERAGE_CLEAR"
        assert len(calls) == 1
    else:
        with pytest.raises((TimeoutError, ValueError)):
            module._refresh_coverage(stage)
        assert len(calls) == (1 if defect in {"late_get", "second_get"} else 0)
    assert sql[0] == "SET LOCAL statement_timeout='1000ms'"
    if defect != "sql_failure":
        assert sql[1] == "SET LOCAL lock_timeout='50ms'"
