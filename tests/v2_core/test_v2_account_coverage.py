import copy
from dataclasses import replace
from uuid import uuid4

import pytest
from test_v2_account_inventory import ReadAccount
from test_v2_protection_child import SCOPE
from test_v2_protection_child import database as database_fixture
from test_v2_protection_child import setup as setup_fixture

from services.v2_testnet_protection_worker import query_permit
from v2_core.account_coverage import AccountCoverageAudit, coverage_from_facts
from v2_core.account_inventory import AccountInventory
from v2_core.state import BusinessState

database = database_fixture
setup = setup_fixture


class Account(ReadAccount):
    account_id = SCOPE.account_id
    environment = SCOPE.environment


def inventory(reader):
    return AccountInventory(reader, scope=SCOPE, clock_ms=lambda: 1000).collect(
        str(uuid4())
    )


@pytest.fixture
def covered(database, setup):
    _, spec, venue, child = setup
    venue.parent["algoStatus"] = "NEW"
    child.protection.query(spec)
    reader = Account()
    for field in ("/fapi/v3/positionRisk",):
        reader.rows[field] = [
            {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.01"}
        ]
    reader.rows["/fapi/v1/openAlgoOrders"] = [copy.deepcopy(venue.parent)]
    audit = AccountCoverageAudit(database, reader, scope=SCOPE, clock_ms=lambda: 1000)
    return audit, reader, spec


def test_owned_position_and_matching_stop_are_diagnostic_only(covered):
    audit, reader, _ = covered
    assert coverage_from_facts(SCOPE, audit.facts(), inventory(reader)) == []
    result = audit.run_once()
    assert result["status"] == "ACCOUNT_COVERAGE_CLEAR"
    assert result["execution_authorized"] is False


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("naked", "MISSING_CONFIRMED_STOP"),
        ("wrongside", "PROTECTION_TERMS_MISMATCH"),
        ("wrongprice", "PROTECTION_TERMS_MISMATCH"),
        ("wrongclient", "PROTECTION_TERMS_MISMATCH"),
        ("notcloseall", "PROTECTION_TERMS_MISMATCH"),
        ("unknownalgo", "UNOWNED_CONDITIONAL_ORDER"),
        ("unknownorder", "UNOWNED_ORDINARY_ORDER"),
        ("quantity", "VENUE_LEDGER_POSITION_MISMATCH"),
        ("external", "VENUE_LEDGER_POSITION_MISMATCH"),
        ("hedge", "ONE_WAY_MODE_REQUIRED"),
        ("unavailable", "INCOMPLETE_ACCOUNT_READ"),
    ],
)
def test_exposure_and_protection_gaps_fail_closed(covered, failure, expected):
    audit, reader, _ = covered
    stop = reader.rows["/fapi/v1/openAlgoOrders"][0]
    if failure == "naked":
        reader.rows["/fapi/v1/openAlgoOrders"] = []
    elif failure == "wrongside":
        stop["side"] = "BUY"
    elif failure == "wrongprice":
        stop["triggerPrice"] = "90"
    elif failure == "wrongclient":
        stop["clientAlgoId"] = "external"
    elif failure == "notcloseall":
        stop["closePosition"] = False
    elif failure == "unknownalgo":
        stop["algoId"] = 1000
    elif failure == "unknownorder":
        reader.rows["/fapi/v1/openOrders"] = [{"symbol": "BTCUSDT", "orderId": 1000}]
    elif failure == "quantity":
        reader.rows["/fapi/v3/positionRisk"][0]["positionAmt"] = "0.02"
    elif failure == "external":
        reader.rows["/fapi/v3/positionRisk"].append(
            {"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "1"}
        )
    elif failure == "hedge":
        reader.rows["/fapi/v1/positionSide/dual"]["dualSidePosition"] = True
    elif failure == "unavailable":
        reader.fail = "/fapi/v3/account"
    assert expected in coverage_from_facts(SCOPE, audit.facts(), inventory(reader))
    assert audit.run_once()["execution_authorized"] is False


def test_multiple_episode_owners_and_unknown_orders_block(covered):
    audit, reader, _ = covered
    facts = audit.facts()
    facts["episodes"].append(
        {**facts["episodes"][0], "episode": str(uuid4()), "remaining": "0.001"}
    )
    facts["orders"][0]["status"] = "UNKNOWN"
    errors = coverage_from_facts(SCOPE, facts, inventory(reader))
    assert "NONEXCLUSIVE_SYMBOL_OWNERSHIP" in errors
    assert "LOCAL_ORDERS_NOT_FINAL" in errors


def test_explicit_external_position_exclusion_is_narrow_and_audited(covered):
    audit, reader, _ = covered
    reader.rows["/fapi/v3/positionRisk"].append(
        {"symbol": "ZORAUSDT", "positionSide": "BOTH", "positionAmt": "26399"}
    )
    scoped = AccountCoverageAudit(
        audit.connect,
        reader,
        scope=SCOPE,
        clock_ms=lambda: 1000,
        excluded_position_symbols=("ZORAUSDT",),
    )
    result = scoped.run_once()
    assert result["status"] == "ACCOUNT_COVERAGE_CLEAR"
    assert result["excluded_position_symbols"] == ["ZORAUSDT"]
    with audit.connect() as conn:
        payload = conn.execute(
            "SELECT payload FROM v2_state_history WHERE reason='ACCOUNT_COVERAGE_AUDIT' AND payload ? 'result' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
    assert payload["result"]["excluded_position_symbols"] == ["ZORAUSDT"]


def test_exclusion_never_hides_local_ownership_or_external_orders(covered):
    audit, reader, _ = covered
    facts = audit.facts()
    facts["episodes"][0]["symbol"] = "ZORAUSDT"
    errors = coverage_from_facts(
        SCOPE,
        facts,
        inventory(reader),
        excluded_position_symbols=("ZORAUSDT",),
    )
    assert "EXCLUDED_POSITION_HAS_LOCAL_OWNERSHIP" in errors
    reader.rows["/fapi/v1/openOrders"] = [{"symbol": "ZORAUSDT", "orderId": 999}]
    errors = coverage_from_facts(
        SCOPE,
        audit.facts(),
        inventory(reader),
        excluded_position_symbols=("ZORAUSDT",),
    )
    assert "UNOWNED_ORDINARY_ORDER" in errors


def test_ledger_change_during_network_blocks(covered, monkeypatch):
    audit, _, _ = covered
    original = audit.facts
    calls = []

    def moving():
        facts = original()
        calls.append(1)
        facts["episodes"][0]["revision"] += len(calls)
        return facts

    monkeypatch.setattr(audit, "facts", moving)
    assert "LOCAL_FACTS_CHANGED_DURING_INVENTORY" in audit.run_once()["blockers"]


def test_scoped_durable_dedup_and_recovery_notification(database, covered):
    audit, reader, _ = covered
    stops = reader.rows["/fapi/v1/openAlgoOrders"]
    reader.rows["/fapi/v1/openAlgoOrders"] = []
    audit.run_once()
    audit.run_once()
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_operational_outbox WHERE event_type='PROTECTION_RECOVERY'"
            ).fetchone()[0]
            == 1
        )
    reader.rows["/fapi/v1/openAlgoOrders"] = stops
    audit.run_once()
    audit.run_once()
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_operational_outbox WHERE event_type='PROTECTION_RECOVERY'"
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'='account-coverage-evidence-v1'"
            ).fetchone()[0]
            == 4
        )
    other = replace(SCOPE, account_id="other")
    with pytest.raises(ValueError):
        coverage_from_facts(other, audit.facts(), inventory(reader))


def test_lock_busy_does_not_read_exchange(database, covered):
    audit, reader, _ = covered
    with database() as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + SCOPE.key,),
        )
        conn.commit()
        assert audit.run_once()["status"] == "BUSY"
    assert not reader.calls


def test_empty_scoped_account_and_live_rejection(database):
    reader = Account()
    audit = AccountCoverageAudit(database, reader, scope=SCOPE, clock_ms=lambda: 1000)
    assert audit.run_once()["status"] == "ACCOUNT_COVERAGE_CLEAR"
    with pytest.raises(ValueError):
        AccountCoverageAudit(
            database,
            reader,
            scope=replace(SCOPE, environment="LIVE"),
            clock_ms=lambda: 1000,
        )


def test_evidence_latest_and_notification_rollback_together(
    database, covered, monkeypatch
):
    audit, reader, _ = covered
    reader.rows["/fapi/v1/openAlgoOrders"] = []
    original = BusinessState.change

    def fail(self, key, **kwargs):
        if key.namespace == "account-coverage-v1":
            raise RuntimeError("checkpoint failure")
        return original(self, key, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(BusinessState, "change", fail)
        with pytest.raises(RuntimeError):
            audit.run_once()
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace' LIKE 'account-coverage%'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) FROM v2_operational_outbox").fetchone()[0]
            == 0
        )
    assert audit.run_once()["status"] == "ACCOUNT_COVERAGE_BLOCKED"


def test_position_change_during_inventory_not_accepted(covered):
    audit, reader, _ = covered
    original = reader.__class__.__call__

    class Moving(Account):
        def __call__(self, method, path, params):
            if path == "/fapi/v3/positionRisk" and path in self.calls:
                return []
            return original(self, method, path, params)

    moving = Moving()
    moving.rows = reader.rows
    errors = coverage_from_facts(SCOPE, audit.facts(), inventory(moving))
    assert "POSITION_CHANGED_DURING_INVENTORY" in errors


def test_unregistered_condition_does_not_count_as_a_stop(covered):
    audit, reader, _ = covered
    facts = audit.facts()
    facts["protections"] = []
    errors = coverage_from_facts(SCOPE, facts, inventory(reader))
    assert "MISSING_CONFIRMED_STOP" in errors
    assert "UNOWNED_CONDITIONAL_ORDER" in errors


@pytest.mark.parametrize(
    "path,total",
    [
        ("/fapi/v1/openOrders", 40),
        ("/fapi/v1/positionSide/dual", 30),
        ("/fapi/v3/positionRisk", 5),
    ],
)
def test_inventory_weight_is_fully_reserved_in_bounded_chunks(path, total):
    class Budget:
        def __init__(self):
            self.calls = []

        def permit(self, weight):
            assert 1 <= weight <= 10
            self.calls.append(weight)
            return True

    budget = Budget()
    assert query_permit(budget, "GET", path, audit_account=True)
    assert sum(budget.calls) == total
    assert not query_permit(budget, "GET", path, audit_account=False)
    assert not query_permit(budget, "POST", path, audit_account=True)
    assert sum(budget.calls) == total


def test_owned_short_with_buy_stop(covered):
    audit, reader, _ = covered
    facts = audit.facts()
    facts["episodes"][0]["side"] = "SELL"
    facts["protections"][0]["payload"]["spec"].update(side="BUY", trigger_price="101")
    reader.rows["/fapi/v3/positionRisk"][0]["positionAmt"] = "-0.01"
    reader.rows["/fapi/v1/openAlgoOrders"][0].update(side="BUY", triggerPrice="101")
    assert coverage_from_facts(SCOPE, facts, inventory(reader)) == []
