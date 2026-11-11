"""Denied work does not consume repeated signed snapshots or relax any fence."""

import json
from uuid import uuid4

import pytest
from test_v2_account_inventory import SCOPE as INVENTORY_SCOPE
from test_v2_account_inventory import ReadAccount
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_lifecycle import opening
from test_v2_directional_settlement import baseline, service
from test_v2_symbol_exit_isolation import build_case, pending_close

from tests.support.v2_admission import database as database_fixture
from v2_core.account_inventory import AccountInventory
from v2_core.transport import ExchangeTransportError

database = database_fixture
closed = closed_fixture


def test_all_exits_held_skip_full_inventory_but_still_recover(database):
    case = build_case(database)
    pending = pending_close(case.runtime, case.btc["intent_id"], exchange_id="802")
    case.stage.audit.run_once = lambda: pytest.fail("pointless full account read")
    result = case.stage.run_once()
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "CLOSE_RECOVERY_PENDING"
    assert result["coverage_skipped"] == "NO_INDEPENDENT_EXIT_CANDIDATE"
    assert len(result["exits"]) == 2
    assert result["execution_authorized"] is False
    assert not case.venue.order_writes
    assert case.runtime.execution.snapshot(pending)["status"] == "ACKNOWLEDGED"
    assert {
        call[2]["symbol"] for call in case.venue.calls if call[1] == "/fapi/v1/order"
    } == {"BTCUSDT", "ETHUSDT"}


def _pending_open(database):
    runtime, result = opening(database, "S8", filled=False, symbol="ETHUSDT")
    oid = result["order_id"]
    assert runtime.data.orders.transition(
        oid, expected_version=1, status="SUBMITTING", evidence={}
    )
    assert runtime.data.orders.transition(
        oid,
        expected_version=2,
        status="ACKNOWLEDGED",
        exchange_order_id="892",
        evidence={"source": "qa acknowledged, fills not observed"},
    )
    return runtime, oid


def test_known_nonfinal_defers_cash_and_inventory_then_recovers(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    other, oid = _pending_open(database)
    stage = service(database, income)
    original = stage.coverage.inventory.collect
    stage.coverage.inventory.collect = lambda _: pytest.fail(
        "cannot settle known local blocker"
    )
    for _ in range(3):
        result = stage.run_once()
        assert result["status"] == "BLOCKED"
        assert result["settlements"][episode] == {
            "status": "BLOCKED",
            "reason": "LOCAL_ORDERS_NOT_FINAL",
            "network_deferred": True,
            "settlement_authorized": False,
        }
    assert income.calls == 0
    assert not runtime.data.trace(episode)["settlements"]
    assert other.data.orders.transition(
        oid,
        expected_version=3,
        status="CANCELLED",
        evidence={"source": "qa terminal no fills", "fills_complete": True},
    )
    stage.coverage.inventory.collect = original
    result = stage.run_once()
    assert result["settlements"][episode]["status"] == "SETTLED"
    assert income.calls > 0


def test_new_nonfinal_after_prefilter_still_blocks_final_settlement(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    stage = service(database, income)
    audit = stage.cash.audit

    def raced(target):
        result = audit(target)
        _pending_open(database)
        return result

    stage.cash.audit = raced
    with pytest.raises(ValueError, match="FINAL_ACCOUNT_NOT_RECONCILED"):
        stage.settle(episode)
    assert not runtime.data.trace(episode)["settlements"]


def test_unsent_prepared_open_does_not_prevent_settlement(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    opening(database, "S8", filled=False, symbol="ETHUSDT")
    result = service(database, income).run_once()
    assert result["settlements"][episode]["status"] == "SETTLED"


@pytest.mark.parametrize("workers", [1, 4])
@pytest.mark.parametrize(
    "error,details",
    [
        (ExchangeTransportError("QUOTA_DENIED"), {"category": "QUOTA_DENIED"}),
        (
            ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=429, code=-1003),
            {
                "category": "EXCHANGE_RESPONSE_ERROR",
                "http_status": 429,
                "exchange_code": -1003,
            },
        ),
        (
            RuntimeError("SECRET-key-signed-url"),
            {"category": "QUERY_OR_VALIDATION_ERROR"},
        ),
        (
            ExchangeTransportError("SECRET-key-signed-url"),
            {"category": "TRANSPORT_ERROR"},
        ),
    ],
)
def test_inventory_preserves_safe_failure_category(workers, error, details):
    class Failed(ReadAccount):
        def __call__(self, *args):
            raise error

    result = AccountInventory(
        Failed(), scope=INVENTORY_SCOPE, clock_ms=lambda: 1000, read_workers=workers
    ).collect(str(uuid4()))
    assert result["status"] == "BLOCKED"
    assert not result["execution_authorized"]
    assert set(result["failure_details"]) == set(result["failures"])
    assert all(value == details for value in result["failure_details"].values())
    assert "SECRET" not in json.dumps(result)
