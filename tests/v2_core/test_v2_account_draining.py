import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import evidence, intent, opened, record

from v2_core.account_draining import AccountDraining
from v2_core.account_registry import AccountRegistry
from v2_core.account_risk import AccountRiskDenied, AccountScope
from v2_core.evidence import DecisionEvidence, canonical
from v2_core.intents import IntentStore
from v2_core.ledger import Ledger
from v2_core.orders import Orders
from v2_core.runner import ExecutionRunner
from v2_core.runtime import DataRuntime

database = database_fixture


@pytest.fixture
def case(database):
    with database() as c:
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260925_tenant_registry.sql"
            ).read_text()
        )
    registry = AccountRegistry(database)
    tenant = registry.create_tenant(uuid4(), "QA")
    other = registry.create_tenant(uuid4(), "Other")
    scope = AccountScope("BINANCE", "test-account", "SANDBOX", "FUTURES")
    account = registry.enroll(
        tenant, scope, display_name="A", credential_ref=uuid4(), request_id=uuid4()
    )["registry_id"]
    return AccountDraining(database), tenant, other, account


def test_drain_blocks_prepare_and_prepared_dispatch_without_network(case, database):
    drain, tenant, _, account = case
    _, orders, order, _ = opened(database)
    result = drain.begin(tenant, account, request_id=uuid4())
    assert result["pending_open_orders"] == {"PREPARED": 1}
    assert result["target_activation_authorized"] is False
    another = replace(intent(), intent_id=str(uuid4()), request_key="another")
    IntentStore(database).admit(another)
    with pytest.raises(AccountRiskDenied, match="ACCOUNT_OPENING_DRAINING"):
        orders.prepare(another.intent_id)
    calls = []
    runner = ExecutionRunner(
        database,
        submit=lambda o: calls.append(o),
        query=lambda _: None,
        risk_check=lambda _: True,
    )
    assert runner.dispatch(order) == "DENIED"
    assert calls == []


def test_prior_submission_permit_remains_unknown_and_query_only(case, database):
    drain, tenant, _, account = case
    _, orders, order, _ = opened(database)
    orders.transition(order, expected_version=1, status="SUBMITTING", evidence={})
    result = drain.begin(tenant, account, request_id=uuid4())
    assert result["existing_permits_pending"] == 1
    runner = ExecutionRunner(
        database,
        submit=lambda _: pytest.fail("must not resend"),
        query=lambda _: None,
        risk_check=lambda _: True,
    )
    assert runner.dispatch(order) == "UNKNOWN"
    assert drain.inspect(tenant, account)["existing_permits_pending"] == 1


def test_confirmed_positions_can_prepare_and_dispatch_reduce_only_close(case, database):
    drain, tenant, _, account = case
    original, orders, order, _ = opened(database)
    orders.transition(order, expected_version=1, status="SUBMITTING", evidence={})
    record(Ledger(database), order, "fill", "100")
    orders.transition(
        order, expected_version=2, status="FILLED", evidence={"fills_complete": True}
    )
    result = drain.begin(tenant, account, request_id=uuid4())
    assert result["venue_flat_verified"] is False and result["switch_complete"] is False
    close, _ = orders.prepare(
        original.intent_id, leg="CLOSE", quantity="0.01", request_key="exit"
    )
    assert orders.transition(
        close, expected_version=1, status="SUBMITTING", evidence={}
    )
    assert (
        ExecutionRunner(
            database,
            submit=lambda _: None,
            query=lambda _: None,
            risk_check=lambda _: True,
        ).snapshot(close)["reduce_only"]
        is True
    )


def test_concurrent_drain_is_monotonic_and_scoped(case, database):
    drain, tenant, other, account = case
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: drain.begin(tenant, account, request_id=uuid4()), range(8)
            )
        )
    assert all(r["gate_version"] == 1 for r in results)
    with pytest.raises(ValueError, match="ACCOUNT_NOT_FOUND"):
        drain.begin(other, account, request_id=uuid4())
    different = replace(intent(), intent_id=str(uuid4()), account_id="account-b")
    IntentStore(database).admit(different)
    Orders(database).prepare(different.intent_id)


def test_submission_and_drain_race_has_explicit_winner(case, database):
    drain, tenant, _, account = case
    _, orders, order, _ = opened(database)

    def dispatch():
        try:
            return orders.transition(
                order, expected_version=1, status="SUBMITTING", evidence={}
            )
        except AccountRiskDenied:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        permit = pool.submit(dispatch)
        stopped = pool.submit(drain.begin, tenant, account, request_id=uuid4())
        allowed, result = permit.result(), stopped.result()
    assert result["existing_permits_pending"] == int(allowed)
    if not allowed:
        with pytest.raises(AccountRiskDenied):
            dispatch_result = orders.transition(
                order, expected_version=1, status="SUBMITTING", evidence={}
            )
            assert not dispatch_result


def test_runtime_records_terminal_rejection_without_retrying_admission(case, database):
    drain, tenant, _, account = case
    drain.begin(tenant, account, request_id=uuid4())
    proof = DecisionEvidence(
        "strategy-v1",
        evidence().config_json,
        canonical(dict(json.loads(evidence().snapshot_json), expires_at_ms=10)),
    )
    request = replace(
        intent(), evidence_ref=proof.evidence_ref, config_digest=proof.config_digest
    )
    runtime = DataRuntime(
        database,
        submit=lambda _: pytest.fail("no venue write"),
        query=lambda _: None,
        risk_check=lambda _: True,
        clock_ms=lambda: 5,
    )
    result = runtime.accept_open(request, proof)
    assert (
        result["status"] == "REJECTED"
        and result["reason"] == "ACCOUNT_OPENING_DRAINING"
    )
    assert runtime.accept_open(request, proof)["status"] == "REJECTED"
    assert runtime.data.trace(request.intent_id)["orders"] == []
