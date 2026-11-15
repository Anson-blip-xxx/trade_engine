"""A native-stop race cannot turn a proven rejection into endless UNKNOWN."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from threading import Barrier

import pytest
from test_v2_bound_order_recovery import (
    Transport,
    absent,
    adapter,
    fill,
    order,
    response,
)
from test_v2_protection_child import SCOPE
from test_v2_protection_child import setup as setup_fixture
from test_v2_submission_diagnostics import FailingSubmit

from tests.support.v2_admission import database as database_fixture
from v2_core.binance import BinanceFutures
from v2_core.runner import ExecutionRunner
from v2_core.transport import ExchangeTransportError

database = database_fixture
setup = setup_fixture


def failure(**changes):
    return {
        "reason": "submission response unavailable",
        "transport": {
            "category": "EXCHANGE_RESPONSE_ERROR",
            "http_status": 400,
            "exchange_code": -2022,
            **changes,
        },
    }


def unknown(**changes):
    return order(
        **{
            "exchange_order_id": None,
            "status": "UNKNOWN",
            "submission_failure": failure(),
            **changes,
        }
    )


def test_synchronous_reduce_only_refusal_is_terminal_without_retry():
    transport = FailingSubmit(
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400, code=-2022)
    )
    result = adapter(transport).submit(order(exchange_order_id=None))
    assert result.status == "REJECTED" and result.exchange_order_id is None
    assert result.evidence == {
        "source": "binance-submit",
        "reason": "VENUE_REDUCE_ONLY_REJECTED",
        "submission_sent": True,
        "transport": failure()["transport"],
    }
    assert result.fills == () and transport.writes == 1


@pytest.mark.parametrize("changes", [{"leg": "OPEN"}, {"reduce_only": False}])
def test_reduce_only_refusal_rule_does_not_expand_to_opening_requests(changes):
    transport = FailingSubmit(
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400, code=-2022)
    )
    with pytest.raises(ExchangeTransportError):
        adapter(transport).submit(order(exchange_order_id=None, **changes))
    assert transport.writes == 1


@pytest.mark.parametrize(
    "category,status,code",
    [
        ("EXCHANGE_RESPONSE_ERROR", 503, -2022),
        ("EXCHANGE_RESPONSE_ERROR", 400, "-2022"),
        ("EXCHANGE_RESPONSE_ERROR", 400, -2022.0),
        ("EXCHANGE_RESPONSE_ERROR", 400, -2013),
        ("EXCHANGE_RESPONSE_ERROR", 400, -1007),
        ("NETWORK_OUTCOME_UNKNOWN", 400, -2022),
        ("INVALID_RESPONSE", 400, -2022),
    ],
)
def test_ambiguous_submit_is_not_reclassified(category, status, code):
    transport = FailingSubmit(
        ExchangeTransportError(category, status=status, code=code)
    )
    with pytest.raises(ExchangeTransportError):
        adapter(transport).submit(order(exchange_order_id=None))
    assert transport.writes == 1


@pytest.mark.parametrize("missing", [absent(), {"code": -2013}])
def test_legacy_unknown_recovers_only_from_its_original_sync_refusal(missing):
    transport = Transport(missing)
    result = adapter(transport).query(unknown())
    assert result.status == "REJECTED" and result.exchange_order_id is None
    assert result.evidence == {
        "source": "binance-recovery",
        "reason": "DURABLE_REDUCE_ONLY_REJECT_CONFIRMED",
        "submission_failure": failure(),
        "query_code": -2013,
    }
    assert result.fills == () and len(transport.calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"submission_failure": {}},
        {"submission_failure": {**failure(), "reason": "not original submit"}},
        {"submission_failure": failure(http_status=503)},
        {"submission_failure": failure(http_status=400.0)},
        {"submission_failure": failure(exchange_code=-2022.0)},
        {"submission_failure": failure(exchange_code="-2022")},
        {"submission_failure": failure(exchange_code=-1007)},
        {"submission_failure": failure(category="NETWORK_OUTCOME_UNKNOWN")},
        {"submission_failure": failure(extra="unexpected")},
        {"leg": "OPEN"},
        {"reduce_only": False},
        {"status": "ACKNOWLEDGED"},
        {"status": "SUBMITTING"},
    ],
)
def test_missing_without_exact_historical_refusal_stays_unknown(changes):
    transport = Transport(absent())
    assert adapter(transport).query(unknown(**changes)) is None
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "missing",
    [
        TimeoutError(),
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=503, code=-2013),
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400, code=-2022),
        None,
    ],
)
def test_recovery_requires_explicit_not_found_not_query_error(missing):
    transport = Transport(missing)
    with pytest.raises((TimeoutError, ExchangeTransportError, ValueError)):
        adapter(transport).query(unknown())
    assert len(transport.calls) == 1


def test_found_order_and_complete_fills_override_historical_reject_evidence():
    transport = Transport(response(), [fill()])
    result = adapter(transport).query(unknown())
    assert result.status == "FILLED" and result.exchange_order_id == "123"
    assert len(result.fills) == 1 and result.evidence["fills_complete"] is True
    assert len(transport.calls) == 2


def test_bound_order_never_relabels_from_rejection_evidence():
    transport = Transport(absent(), absent())
    result = adapter(transport).query(unknown(exchange_order_id="123"))
    assert result.status == "UNKNOWN" and result.exchange_order_id == "123"
    assert len(transport.calls) == 2


def close_runner(database, setup):
    data, spec, _, _ = setup
    close, _ = data.orders.prepare(
        spec.episode, leg="CLOSE", quantity="0.01", request_key="exit:race"
    )

    def old_submit(_):
        raise ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400, code=-2022)

    execution = ExecutionRunner(
        database,
        submit=old_submit,
        query=lambda _: None,
        risk_check=lambda _: True,
        scope=SCOPE,
    )
    assert execution.dispatch(close) == "UNKNOWN"
    transport = Transport(absent())
    venue = BinanceFutures(
        transport, account_id=SCOPE.account_id, environment="SANDBOX"
    )
    execution.query = venue.query
    return data, close, execution, transport


def test_flat_native_stop_episode_recovers_via_normal_batch_without_resubmit(
    database, setup
):
    data, close, execution, transport = close_runner(database, setup)
    native = setup[3].reconcile(setup[1])
    assert native["status"] == "FILLED"
    trace_before = data.trace(setup[1].episode)
    assert (
        sum(
            Decimal(f["quantity"])
            * (
                1
                if next(
                    o["leg"]
                    for o in trace_before["orders"]
                    if o["order_id"] == f["order_id"]
                )
                == "OPEN"
                else -1
            )
            for f in trace_before["fills"]
        )
        == 0
    )
    execution.submit = lambda _: pytest.fail("recovery attempted another order")
    assert execution.recover_batch(limit=10) == {close: "REJECTED"}
    assert execution.recover(close) == "REJECTED"
    assert execution.recover_batch(limit=10) == {}
    trace_after = data.trace(setup[1].episode)
    assert trace_after["fills"] == trace_before["fills"]
    assert len(trace_after["orders"]) == 3
    assert all(o["status"] in {"FILLED", "REJECTED"} for o in trace_after["orders"])
    assert len(transport.calls) == 1
    with database() as conn:
        events = conn.execute(
            "SELECT status,evidence FROM v2_order_events WHERE order_id=%s ORDER BY version",
            (close,),
        ).fetchall()
    assert [row[0] for row in events] == [
        "PREPARED",
        "SUBMITTING",
        "UNKNOWN",
        "REJECTED",
    ]
    assert events[2][1] == failure()
    assert events[3][1]["submission_failure"] == failure()


def test_confirmed_fills_block_historical_reclassification(database, setup):
    data, close, execution, _ = close_runner(database, setup)
    data.ledger.record_fill(
        order_id=close,
        exchange_fill_id="part-close",
        quantity="0.005",
        price="99",
        fee="0.01",
        fee_currency="USDT",
        occurred_at_ms=2,
        evidence={"source": "test"},
    )
    with pytest.raises(ValueError, match="cannot reject an order with confirmed fills"):
        execution.recover(close)
    assert execution.snapshot(close)["status"] == "UNKNOWN"


@pytest.mark.parametrize("next_status", ["UNKNOWN", "ACKNOWLEDGED"])
def test_concurrent_binding_cannot_be_overwritten_by_stale_absence(
    database, setup, next_status
):
    data, close, execution, _ = close_runner(database, setup)
    venue_query = execution.query

    def raced_query(current):
        result = venue_query(current)
        assert data.orders.transition(
            close,
            expected_version=current["version"],
            status=next_status,
            exchange_order_id="777",
            evidence={"source": "new authoritative lookup"},
        )
        return result

    execution.query = raced_query
    with pytest.raises(ValueError, match="DURABLE_REDUCE_ONLY_REJECT_CONFLICT"):
        execution.recover(close)
    current = execution.snapshot(close)
    assert current["status"] == next_status and current["exchange_order_id"] == "777"


def test_changed_failure_cannot_borrow_prior_rejection_proof(database, setup):
    _, close, execution, _ = close_runner(database, setup)
    observation = execution.query(execution.snapshot(close))
    evidence = deepcopy(observation.evidence)
    evidence["submission_failure"]["transport"]["exchange_code"] = -1007
    from dataclasses import replace

    with pytest.raises(ValueError, match="DURABLE_REDUCE_ONLY_REJECT_CONFLICT"):
        execution._apply(close, replace(observation, evidence=evidence))
    assert execution.snapshot(close)["status"] == "UNKNOWN"


def test_two_workers_recover_same_rejection_idempotently(database, setup):
    _, close, execution, transport = close_runner(database, setup)
    transport.responses.append(absent())
    queried = Barrier(2)
    venue_query = execution.query

    def overlapping_query(current):
        result = venue_query(current)
        assert current["status"] == "UNKNOWN"
        queried.wait(timeout=10)
        return result

    execution.query = overlapping_query
    execution.submit = lambda _: pytest.fail("recovery attempted another order")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execution.recover, [close, close]))
    assert results == ["REJECTED", "REJECTED"]
    assert len(transport.calls) == 2
    with database() as conn:
        events = conn.execute(
            "SELECT status FROM v2_order_events WHERE order_id=%s ORDER BY version",
            (close,),
        ).fetchall()
    assert [row[0] for row in events] == [
        "PREPARED",
        "SUBMITTING",
        "UNKNOWN",
        "REJECTED",
    ]


def test_altered_proof_cannot_replay_already_committed_rejection(database, setup):
    _, close, execution, _ = close_runner(database, setup)
    observation = execution.query(execution.snapshot(close))
    assert execution._apply(close, observation) == "REJECTED"
    assert execution._apply(close, observation) == "REJECTED"
    evidence = deepcopy(observation.evidence)
    evidence["query_code"] = -1007
    from dataclasses import replace

    with pytest.raises(ValueError, match="DURABLE_REDUCE_ONLY_REJECT_CONFLICT"):
        execution._apply(close, replace(observation, evidence=evidence))
    assert execution.snapshot(close)["status"] == "REJECTED"
