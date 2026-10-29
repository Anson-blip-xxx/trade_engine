"""Real PG state-machine regression for the missing-stop exit deadlock."""


import pytest
from test_v2_directional_exit_stage import setup_exit
from test_v2_intent_admission import database as database_fixture

from v2_core.runtime_policy import resolve
from v2_core.transport import ExchangeTransportError

database = database_fixture


def naked(database, **kwargs):
    runtime, result, venue, service = setup_exit(database, **kwargs)
    spec = service.stop.plan(result["order_id"])
    snap, payload = service.protection.read(spec)
    payload = {
        **payload,
        "status": "UNKNOWN",
        "submission_error": {"class": "ExchangeTransportError"},
    }
    payload.pop("observation", None)
    payload.pop("submission", None)
    service.protection.save(spec, snap, payload)
    venue.parent = None
    venue.rows["/fapi/v1/openAlgoOrders"] = []
    return runtime, result, venue, service


@pytest.mark.parametrize("strategy,side", [("S6", "SELL"), ("S8", "BUY")])
def test_missing_stop_reduces_without_indicators_or_repeating_unknown_post(
    database, strategy, side
):
    _, result, venue, service = naked(
        database, allow_writes=True, strategy=strategy
    )
    service.observe = lambda _: pytest.fail(
        "Emergency exit must not depend on indicators"
    )
    outcome = service.run_once()
    emergency = outcome["emergency_exits"][result["intent_id"]]
    assert outcome["status"] == "BLOCKED"  # New openings remain fenced.
    assert emergency["outcome"] == "ACKNOWLEDGED"
    assert len(venue.order_writes) == 1
    assert venue.order_writes[0]["side"] == side
    assert venue.order_writes[0]["reduceOnly"] == "true"
    assert venue.order_writes[0]["quantity"] == "1.25"
    assert len(venue.writes) == 1  # Only the original protection POST.
    again = service.run_once()
    assert again["status"] == "BLOCKED"
    assert again["reason"] == "CLOSE_RECOVERY_PENDING"
    assert len(venue.order_writes) == 1


def test_no_emergency_write_without_explicit_permission(database):
    _, result, venue, service = naked(database, allow_writes=False)
    answer = service.run_once()
    assert answer["emergency_exits"][result["intent_id"]]["status"] == "ACTION_DISABLED"
    assert venue.order_writes == []


def test_confirmation_grace_remains_bounded_and_configurable(database):
    _, result, venue, service = naked(database, allow_writes=True, now=1001)
    answer = service.run_once()
    assert (
        answer["emergency_exits"][result["intent_id"]]["reason"]
        == "PROTECTION_CONFIRMATION_GRACE"
    )
    assert not venue.order_writes
    assert resolve()["protection.unconfirmed_grace_ms"] == 30000
    with pytest.raises(ValueError):
        resolve({"protection.unconfirmed_grace_ms": -1})


@pytest.mark.parametrize("bad_quantity", ["0", ".5", "-1.25"])
def test_mismatch_cannot_be_masked_by_missing_protection(database, bad_quantity):
    _, _, venue, service = naked(database, allow_writes=True)
    venue.rows["/fapi/v3/positionRisk"][0]["positionAmt"] = bad_quantity
    answer = service.run_once()
    assert answer["status"] == "BLOCKED"
    assert "VENUE_LEDGER_POSITION_MISMATCH" in answer["coverage"]["blockers"]
    assert not answer["emergency_exits"]
    assert not venue.order_writes


def test_existing_protection_is_not_an_emergency_close_target(database):
    _, result, venue, service = setup_exit(database, allow_writes=True)
    assert service._unprotected_exit(result["intent_id"])["status"] == "BLOCKED"
    assert not venue.order_writes


def test_inventory_failure_is_never_a_reduce_permit(database):
    _, _, venue, service = naked(database, allow_writes=True)
    original = service.audit.inventory.request

    def failed(method, path, params):
        if path == "/fapi/v3/positionRisk":
            raise ExchangeTransportError("NETWORK_OUTCOME_UNKNOWN")
        return original(method, path, params)

    service.audit.inventory.request = failed
    assert service.run_once()["status"] == "BLOCKED"
    assert not venue.order_writes


def test_external_binding_cannot_be_submitted_even_if_called_directly():
    from v2_core.binance import BinanceFutures
    from v2_core.errors import SubmissionNotSent

    adapter = BinanceFutures(
        lambda *_: pytest.fail("No venue IO allowed"),
        account_id="test",
        environment="SANDBOX",
    )
    with pytest.raises(SubmissionNotSent):
        adapter.submit({"request_evidence": {"origin": "BINANCE_EXTERNAL_CLOSE"}})
