"""No-send classification, ambiguous protection safety and external-close proof."""

from dataclasses import asdict

import pytest
from test_v2_protection import SPEC, Venue
from test_v2_protection import SCOPE as PROTOCOL_SCOPE
from test_v2_protection_child import SCOPE, database, setup

from v2_core.protection import TestnetProtection, protection_error_evidence
from v2_core.protection_supervisor import ProtectionSupervisor
from v2_core.service import TradingData
from v2_core.state import BusinessState, StateKey
from v2_core.transport import ExchangeTransportError

__all__ = ["database", "setup"]


@pytest.mark.parametrize("category", ["QUOTA_DENIED", "ENDPOINT_OR_WRITE_DISABLED"])
def test_pre_send_gate_is_durable_not_sent_without_read_or_retry(database, category):
    class Denied(Venue):
        calls = 0

        def __call__(self, method, path, params):
            self.calls += 1
            assert method == "POST"
            raise ExchangeTransportError(category)

    venue = Denied()
    worker = TestnetProtection(BusinessState(database), venue, scope=PROTOCOL_SCOPE)
    for _ in range(3):
        result = worker.submit_once(SPEC)
        assert result["status"] == "NOT_SENT"
        assert result["error"]["category"] == category
    assert worker.query(SPEC)["status"] == "NOT_SENT"
    assert venue.calls == 1
    assert worker.read(SPEC)[1]["status"] == "NOT_SENT"


def test_ambiguous_post_missing_order_never_resubmits_or_fivefold_polls(database):
    class Missing(Venue):
        methods = []

        def __call__(self, method, path, params):
            self.methods.append(method)
            if method == "POST":
                raise ExchangeTransportError("NETWORK_OUTCOME_UNKNOWN")
            raise ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400, code=-2013)

    venue = Missing()
    worker = TestnetProtection(BusinessState(database), venue, scope=PROTOCOL_SCOPE)
    for _ in range(3):
        with pytest.raises(ExchangeTransportError) as caught:
            worker.submit_once(SPEC)
        assert caught.value.code == -2013
    assert venue.methods == ["POST", "GET", "GET", "GET"]
    payload = worker.read(SPEC)[1]
    assert payload["status"] == "UNKNOWN"
    assert payload["submission_error"]["category"] == "NETWORK_OUTCOME_UNKNOWN"


def test_error_diagnostics_never_persist_exception_text_or_unsafe_attributes():
    exc = ExchangeTransportError("secret-url", status="private-header", code={"secret": "key"})
    exc.diagnostic_evidence = lambda: {"secret": "not trusted"}
    result = protection_error_evidence(exc)
    assert result == {
        "class": "ExchangeTransportError", "category": "TRANSPORT_ERROR",
        "code": None, "http_status": None,
    }
    assert protection_error_evidence(TimeoutError("private-url")) == {"class": "TimeoutError"}


def test_supervisor_missing_parent_retains_safe_exchange_diagnostic(database, setup):
    _, spec, venue, child = setup

    def absent(method, path, params):
        assert method == "GET"
        raise ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400, code=-2013)

    absent.account_id, absent.environment = SCOPE.account_id, SCOPE.environment
    worker = ProtectionSupervisor(database, absent, scope=SCOPE)
    result = worker.run_once()["results"][child.protection.key(spec).identity]
    assert result == {
        "status": "RECOVERY_PENDING", "error_code": "ExchangeTransportError",
        "diagnostic": {
            "class": "ExchangeTransportError", "category": "EXCHANGE_RESPONSE_ERROR",
            "code": -2013, "http_status": 400,
        },
    }
    assert not venue.calls


def test_supervisor_proven_no_send_reports_unprotected_without_venue_get(database, setup):
    _, spec, venue, child = setup
    snapshot, payload = child.protection.read(spec)
    payload.update(status="NOT_SENT", submission_error={"category": "QUOTA_DENIED"})
    child.protection.save(spec, snapshot, payload)
    result = ProtectionSupervisor(database, venue, scope=SCOPE).run_once()["results"]
    assert result[child.protection.key(spec).identity] == {
        "status": "UNPROTECTED", "parent_status": "NOT_SENT",
    }
    assert not venue.calls


def resolution_fixture(database, setup, *, apply_close=True):
    data, spec, venue, child = setup
    if apply_close:
        child.reconcile(spec)
    trace = data.trace(spec.episode)
    closes = [order["order_id"] for order in trace["orders"] if order["leg"] == "CLOSE"]
    key = StateKey(**asdict(SCOPE), namespace="external-close-resolution-v1", key=spec.episode)
    proof = {
        "status": "CONFIRMED", "account_scope": asdict(SCOPE),
        "episode": spec.episode, "symbol": spec.symbol,
        "exchange_order_ids": ["555"], "close_order_ids": closes,
        "remaining_quantity": "0", "observed_at_ms": 4,
        "inventory": {
            "observation_id": "qa", "started_at_ms": 3, "finished_at_ms": 4,
            "response_digests": {},
            "summary": {"positions": [], "ordinary_orders": [], "conditional_orders": []},
        },
    }
    snapshot, payload = child.protection.read(spec)
    payload.update(
        status="EXTERNAL_FLAT_RECONCILED",
        external_reconciliation={
            "state_id": key.identity, "account_scope": asdict(SCOPE),
            "episode": spec.episode, "symbol": spec.symbol,
        },
    )
    child.protection.save(spec, snapshot, payload)
    venue.calls.clear()
    return key, proof


def save_resolution(database, key, proof):
    assert BusinessState(database).change(
        key, expected_version=0, request_key="external-close", payload=proof,
        reason="QA_EXTERNAL_CLOSE",
    ).code == "APPLIED"


def test_external_flat_marker_validates_resolution_and_ledger_without_parent_query(database, setup):
    _, spec, venue, child = setup
    key, proof = resolution_fixture(database, setup)
    save_resolution(database, key, proof)
    worker = ProtectionSupervisor(database, venue, scope=SCOPE)
    expected = {"status": "TERMINAL_FLAT_LEDGER", "resolution_state_id": key.identity}
    state_id = child.protection.key(spec).identity
    assert worker.run_once()["results"][state_id] == expected
    assert worker.run_once()["results"][state_id] == expected
    assert not venue.calls
    assert child.protection.query(spec) == {"status": "EXTERNAL_FLAT_RECONCILED"}
    with database() as conn:
        conn.execute("UPDATE v2_episodes SET status='SETTLED' WHERE episode_id=%s", (spec.episode,))
    assert worker.run_once()["results"] == {}
    assert not venue.calls


@pytest.mark.parametrize(
    "bad", ["missing", "scope", "symbol", "pending", "close_ids", "exchange_ids", "inventory", "deleted"]
)
def test_invalid_external_resolution_stays_blocked_without_fabricating_cancel(
    database, setup, bad, monkeypatch
):
    data, spec, venue, child = setup
    key, proof = resolution_fixture(database, setup)
    if bad == "scope":
        proof["account_scope"]["account_id"] = "other"
    elif bad == "symbol":
        proof["symbol"] = "ETHUSDT"
    elif bad == "close_ids":
        proof["close_order_ids"] = [next(o["order_id"] for o in data.trace(spec.episode)["orders"] if o["leg"] == "OPEN")]
    elif bad == "exchange_ids":
        proof["exchange_order_ids"] = ["999"]
    elif bad == "inventory":
        proof["inventory"]["summary"]["conditional_orders"] = [{"symbol": spec.symbol}]
    elif bad == "pending":
        # Inject an unresolved read observation without bypassing immutable
        # database order/event/version guards to manufacture an illegal state.
        original_trace = TradingData.trace

        def pending_trace(reader, *args, **kwargs):
            result = original_trace(reader, *args, **kwargs)
            if result and result["intent_id"] == spec.episode:
                for order in result["orders"]:
                    if order["order_id"] == proof["close_order_ids"][0]:
                        order["status"] = "UNKNOWN"
            return result

        monkeypatch.setattr(TradingData, "trace", pending_trace)
    if bad != "missing":
        save_resolution(database, key, proof)
    if bad == "deleted":
        BusinessState(database).change(key, expected_version=1, request_key="delete", payload={}, reason="QA", deleted=True)
    result = ProtectionSupervisor(database, venue, scope=SCOPE).run_once()["results"][child.protection.key(spec).identity]
    assert result["status"] == "RECOVERY_PENDING"
    assert not venue.calls
    assert child.protection.read(spec)[1]["status"] == "EXTERNAL_FLAT_RECONCILED"


def test_external_resolution_cannot_hide_positive_ledger_exposure(database, setup):
    _, spec, venue, child = setup
    venue.child.update(origQty="0.005", executedQty="0.005")
    venue.parent["actualQty"] = "0.005"
    venue.trades[0]["qty"] = "0.005"
    key, proof = resolution_fixture(database, setup)
    save_resolution(database, key, proof)
    result = ProtectionSupervisor(database, venue, scope=SCOPE).run_once()["results"][child.protection.key(spec).identity]
    assert result["status"] == "RECOVERY_PENDING"
    assert not venue.calls
