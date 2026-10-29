import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import pytest
from test_v2_account_risk import SCOPE, configured, prepare, runner
from test_v2_intent_admission import database as database_fixture

from v2_core.recovery_diagnostics import NAMESPACE, query_proof, record, summaries
from v2_core.runner import ExchangeObservation
from v2_core.transport import ExchangeTransportError

database = database_fixture


def proof():
    return {
        "bound_exchange_order_id": True,
        "attempts": [
            {
                "method": method,
                "outcome": "ABSENT",
                "transport": {
                    "category": "EXCHANGE_RESPONSE_ERROR",
                    "http_status": 400,
                    "exchange_code": -2013,
                },
            }
            for method in ("CLIENT_ORDER_ID", "EXCHANGE_ORDER_ID")
        ],
    }


def observation(current):
    return ExchangeObservation(
        current["client_order_id"],
        "UNKNOWN",
        current["exchange_order_id"],
        evidence={"recovery_query": proof()},
    )


def ready(database):
    risk = configured(database)
    _, _, oid, _ = prepare(database)
    execution = runner(database)
    assert execution.dispatch(oid) == "ACKNOWLEDGED"
    execution.query = observation
    return risk, execution, oid


def diagnostic(database, oid):
    with database() as conn:
        return conn.execute(
            """SELECT s.payload,s.version,h.created_at
            FROM v2_business_state s JOIN v2_state_history h
              ON h.state_id=s.state_id AND h.version=s.version
            WHERE s.scope->>'namespace'=%s AND s.scope->>'key'=%s""",
            (NAMESPACE, oid),
        ).fetchone()


def test_missing_bound_order_keeps_ack_reserve_and_deduplicates(database):
    risk, execution, oid = ready(database)
    before = execution.snapshot(oid)
    assert execution.recover(oid) == "UNKNOWN"
    saved = diagnostic(database, oid)
    assert saved[0]["outcome"] == "BOUND_ORDER_LOOKUPS_ABSENT"
    assert saved[0]["query"] == proof()
    assert saved[1] == 1 and saved[2] is not None
    for _ in range(3):
        assert execution.recover(oid) == "UNKNOWN"
    assert diagnostic(database, oid) == saved
    assert execution.snapshot(oid) == before
    assert risk.usage(SCOPE)["positions"] == 1
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_fills").fetchone() == (0,)


def test_concurrent_identical_query_diagnostics_have_one_history_event(database):
    _, execution, oid = ready(database)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert (
            list(pool.map(lambda _: execution.recover(oid), range(4)))
            == ["UNKNOWN"] * 4
        )
    assert diagnostic(database, oid)[1] == 1


def test_contended_diagnostic_lock_does_not_stall_unknown_recovery(database, caplog):
    _, execution, oid = ready(database)
    from v2_core.ledger import lock_order_episode

    with database() as conn:
        lock_order_episode(conn, oid)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(execution.recover, oid)
            # Recovery must return while this independent transaction still
            # owns the episode lock, not wait for the normal 5 s lock budget.
            assert result.result(timeout=3) == "UNKNOWN"
    assert execution.snapshot(oid)["status"] == "ACKNOWLEDGED"
    assert diagnostic(database, oid) is None
    assert "ORDER_RECOVERY_DIAGNOSTIC_UNAVAILABLE" in caplog.text


def test_failed_query_is_redacted_and_does_not_alter_order(database):
    _, execution, oid = ready(database)
    before = execution.snapshot(oid)

    def fail(_):
        raise ExchangeTransportError("secret URL credential", status=429, code=-1003)

    execution.query = fail
    with pytest.raises(ExchangeTransportError):
        execution.recover(oid)
    payload = diagnostic(database, oid)[0]
    assert payload["transport"] == {
        "category": "TRANSPORT_ERROR",
        "http_status": 429,
        "exchange_code": -1003,
    }
    assert "secret" not in json.dumps(payload)
    assert execution.snapshot(oid) == before


def test_none_is_not_falsely_labeled_binance_2013(database):
    _, execution, oid = ready(database)
    execution.query = lambda _: None
    assert execution.recover(oid) == "UNKNOWN"
    assert diagnostic(database, oid)[0]["outcome"] == "NO_OBSERVATION"
    assert "2013" not in json.dumps(diagnostic(database, oid)[0])


@pytest.mark.parametrize(
    "changes",
    [
        {"client_order_id": "different"},
        {"exchange_order_id": "different"},
        {"status": "FILLED"},
        {"fills": ({"quantity": "1"},)},
    ],
)
def test_invalid_missing_proof_cannot_change_order_or_record_fills(database, changes):
    _, execution, oid = ready(database)
    before = execution.snapshot(oid)
    execution.query = lambda current: replace(observation(current), **changes)
    with pytest.raises(ValueError):
        execution.recover(oid)
    assert execution.snapshot(oid) == before
    assert diagnostic(database, oid) is None


def test_query_finding_order_is_applied_and_diagnostic_updated(database):
    _, execution, oid = ready(database)
    assert execution.recover(oid) == "UNKNOWN"

    def found(current):
        report = proof()
        report["attempts"][-1].update(outcome="FOUND", transport={})
        return replace(
            observation(current),
            status="CANCELLED",
            evidence={"recovery_query": report, "fills_complete": True},
        )

    execution.query = found
    assert execution.recover(oid) == "CANCELLED"
    assert diagnostic(database, oid)[0]["outcome"] == "BOUND_ORDER_LOOKUP_FOUND"
    assert diagnostic(database, oid)[1] == 2
    assert execution.recover(oid) == "CANCELLED"


def fill_observation(current, *, quantity="0.01", status="FILLED", evidence=None):
    return ExchangeObservation(
        current["client_order_id"],
        status,
        current["exchange_order_id"],
        fills=(
            {
                "exchange_fill_id": "qa-normal-query-fill",
                "quantity": quantity,
                "price": "100",
                "fee": "0.01",
                "fee_currency": "USDT",
                "occurred_at_ms": 100,
                "evidence": {"source": "qa"},
            },
        ),
        evidence={"fills_complete": True} if evidence is None else evidence,
    )


@pytest.mark.parametrize("initial", ["ABSENT", "FAILED"])
@pytest.mark.parametrize("partial_fill", [False, True])
def test_normal_ack_supersedes_same_version_missing_or_failed_query(
    database, initial, partial_fill
):
    risk, execution, oid = ready(database)
    before = execution.snapshot(oid)
    if initial == "ABSENT":
        assert execution.recover(oid) == "UNKNOWN"
    else:

        def failed(_):
            raise ExchangeTransportError("QUOTA_DENIED")

        execution.query = failed
        with pytest.raises(ExchangeTransportError):
            execution.recover(oid)
    assert diagnostic(database, oid)[1] == 1

    def normal(current):
        if partial_fill:
            return fill_observation(current, quantity="0.004", status="ACKNOWLEDGED")
        return ExchangeObservation(
            current["client_order_id"],
            "ACKNOWLEDGED",
            current["exchange_order_id"],
            evidence={"source": "qa normal query"},
        )

    execution.query = normal
    assert execution.recover(oid) == "ACKNOWLEDGED"
    saved = diagnostic(database, oid)
    assert saved[0]["outcome"] == "QUERY_OBSERVED"
    assert saved[0]["order_version"] == before["version"]
    assert "query" not in saved[0] and "transport" not in saved[0]
    assert saved[1] == 2
    assert execution.snapshot(oid)["version"] == before["version"]
    with database() as conn:
        report = summaries(conn, SCOPE, [(oid, before["version"])])
        assert report[oid]["outcome"] == "QUERY_OBSERVED"
        assert "query" not in report[oid]
        assert conn.execute(
            "SELECT count(*) FROM v2_fills WHERE order_id=%s", (oid,)
        ).fetchone() == (int(partial_fill),)
    assert risk.usage(SCOPE)["positions"] == 1
    assert execution.recover(oid) == "ACKNOWLEDGED"
    assert diagnostic(database, oid) == saved


@pytest.mark.parametrize("filled", [False, True])
def test_normal_query_without_previous_diagnostic_does_not_create_one(database, filled):
    _, execution, oid = ready(database)

    def normal(current):
        if filled:
            return fill_observation(current)
        return ExchangeObservation(
            current["client_order_id"],
            "ACKNOWLEDGED",
            current["exchange_order_id"],
            evidence={"source": "qa normal query"},
        )

    execution.query = normal
    assert execution.recover(oid) == ("FILLED" if filled else "ACKNOWLEDGED")
    assert diagnostic(database, oid) is None
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'=%s",
            (NAMESPACE,),
        ).fetchone() == (0,)


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("next_status", ["UNKNOWN", "ACKNOWLEDGED"])
def test_applied_query_cannot_borrow_a_concurrently_updated_order_version(
    database, monkeypatch, fallback, next_status
):
    _, execution, oid = ready(database)
    assert execution.recover(oid) == "UNKNOWN"
    previous = diagnostic(database, oid)
    apply = execution._apply

    def changed_after_apply(order_id, value):
        result = apply(order_id, value)
        current = execution.snapshot(order_id)
        assert execution.orders.transition(
            order_id,
            expected_version=current["version"],
            status="UNKNOWN",
            evidence={"reason": "qa newer concurrent evidence"},
        )
        if next_status == "ACKNOWLEDGED":
            current = execution.snapshot(order_id)
            assert execution.orders.transition(
                order_id,
                expected_version=current["version"],
                status="ACKNOWLEDGED",
                evidence={"source": "qa newer concurrent query"},
            )
        return result

    def found(current):
        evidence = {"source": "qa normal query"}
        if fallback:
            report = proof()
            report["attempts"][-1].update(outcome="FOUND", transport={})
            evidence["recovery_query"] = report
        return ExchangeObservation(
            current["client_order_id"],
            "ACKNOWLEDGED",
            current["exchange_order_id"],
            evidence=evidence,
        )

    monkeypatch.setattr(execution, "_apply", changed_after_apply)
    execution.query = found
    assert execution.recover(oid) == "ACKNOWLEDGED"
    current = execution.snapshot(oid)
    assert current["status"] == next_status
    assert diagnostic(database, oid) == previous
    with database() as conn:
        assert summaries(conn, SCOPE, [(oid, current["version"])]) == {}


def test_normal_fill_commits_even_when_superseding_old_diagnostic_fails(
    database, monkeypatch
):
    _, execution, oid = ready(database)
    assert execution.recover(oid) == "UNKNOWN"
    previous = diagnostic(database, oid)

    def unavailable(*_):
        raise PermissionError("secret diagnostic ACL")

    execution.query = fill_observation
    monkeypatch.setattr("v2_core.runner.record_recovery", unavailable)
    assert execution.recover(oid) == "FILLED"
    assert execution.recover(oid) == "FILLED"
    assert diagnostic(database, oid) == previous
    current = execution.snapshot(oid)
    with database() as conn:
        assert summaries(conn, SCOPE, [(oid, current["version"])]) == {}
        assert conn.execute(
            "SELECT count(*) FROM v2_fills WHERE order_id=%s", (oid,)
        ).fetchone() == (1,)


def test_diagnostic_failure_cannot_mask_original_query_error(
    database, monkeypatch, caplog
):
    _, execution, oid = ready(database)
    original = ExchangeTransportError("NETWORK_OUTCOME_UNKNOWN")

    def unavailable(*_):
        raise RuntimeError("credential must never appear in log")

    def query(_):
        raise original

    monkeypatch.setattr("v2_core.runner.record_recovery", unavailable)
    execution.query = query
    for _ in range(2):
        with pytest.raises(ExchangeTransportError) as caught:
            execution.recover(oid)
        assert caught.value is original
    assert caplog.text.count("ORDER_RECOVERY_DIAGNOSTIC_UNAVAILABLE") == 1
    assert "credential" not in caplog.text


def test_confirmed_fill_commits_even_when_diagnostic_storage_fails(
    database, monkeypatch
):
    _, execution, oid = ready(database)

    def unavailable(*_):
        raise PermissionError("diagnostic ACL")

    def found(current):
        report = proof()
        report["attempts"][-1].update(outcome="FOUND", transport={})
        return replace(
            observation(current),
            status="FILLED",
            fills=(
                {
                    "exchange_fill_id": "qa-recovered-fill",
                    "quantity": "0.01",
                    "price": "100",
                    "fee": "0.01",
                    "fee_currency": "USDT",
                    "occurred_at_ms": 100,
                    "evidence": {"source": "qa"},
                },
            ),
            evidence={"recovery_query": report, "fills_complete": True},
        )

    monkeypatch.setattr("v2_core.runner.record_recovery", unavailable)
    execution.query = found
    assert execution.recover(oid) == "FILLED"
    assert execution.recover(oid) == "FILLED"
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_fills WHERE order_id=%s", (oid,)
        ).fetchone() == (1,)
    assert diagnostic(database, oid) is None


def test_stale_query_diagnostic_cannot_overwrite_newer_order_state(database):
    _, execution, oid = ready(database)
    before = execution.snapshot(oid)
    execution.orders.transition(
        oid,
        expected_version=before["version"],
        status="CANCELLED",
        evidence={"fills_complete": True},
    )
    assert record(database, before, {"outcome": "NO_OBSERVATION"}) == "STALE"
    assert diagnostic(database, oid) is None


def test_health_summaries_are_scoped_versioned_and_do_not_export_raw_payload(database):
    _, execution, oid = ready(database)
    current = execution.snapshot(oid)
    with pytest.raises(ValueError, match="INVALID_RECOVERY_DIAGNOSTIC"):
        record(database, current, {"outcome": "QUERY_FAILED", "raw": "secret"})
    assert (
        record(
            database,
            current,
            {"outcome": "QUERY_FAILED", "transport": {"category": "QUOTA_DENIED"}},
        )
        == "APPLIED"
    )
    with database() as conn:
        report = summaries(conn, SCOPE, [(oid, current["version"])])
        assert report[oid]["outcome"] == "QUERY_FAILED"
        assert report[oid]["first_observed_at_ms"] > 0
        assert "secret" not in json.dumps(report)
        assert (
            summaries(
                conn, replace(SCOPE, account_id="another"), [(oid, current["version"])]
            )
            == {}
        )
        assert summaries(conn, SCOPE, [(oid, current["version"] + 1)]) == {}


def test_health_summary_shows_both_missing_identity_checks(database):
    from services.v2_trading_health import TradingHealth

    _, execution, oid = ready(database)
    with database() as conn:
        conn.execute(
            "UPDATE v2_orders SET version=version+1,updated_at=clock_timestamp()-interval '100 seconds' WHERE order_id=%s",
            (oid,),
        )
    assert execution.recover(oid) == "UNKNOWN"
    health = TradingHealth(database, account_id=SCOPE.account_id)
    assert "ORDER_PROGRESS_STALLED" in health()
    saved = json.loads(health.store.read(health.key).payload_json)
    stalled = saved["findings"]["ORDER_PROGRESS_STALLED"]["orders"]
    assert stalled[0]["recovery_diagnostic"]["query"] == proof()


def test_health_summary_bounds_reads_before_database_access():
    with pytest.raises(ValueError, match="RECOVERY_DIAGNOSTIC_READ_BOUND"):
        summaries(None, SCOPE, [("unused", 1)] * 21)


def test_health_auxiliary_sql_timeout_restores_caller_transaction(database):
    _, execution, oid = ready(database)
    assert execution.recover(oid) == "UNKNOWN"
    version = execution.snapshot(oid)["version"]

    def read():
        with database() as conn:
            conn.execute("SET LOCAL statement_timeout='5s'")
            conn.execute("SET LOCAL lock_timeout='2s'")
            report = summaries(conn, SCOPE, [(oid, version)])
            assert report == {oid: {"evidence_status": "UNAVAILABLE"}}
            assert conn.execute("SELECT 1").fetchone() == (1,)
            assert conn.execute(
                "SELECT current_setting('statement_timeout'),current_setting('lock_timeout')"
            ).fetchone() == ("5s", "2s")

    with database() as conn:
        conn.execute("LOCK TABLE v2_state_history IN ACCESS EXCLUSIVE MODE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(read).result(timeout=3)


def test_exception_subclass_cannot_export_secret_or_mask_query_error(database):
    class UntrustedError(ExchangeTransportError):
        def diagnostic_evidence(self):
            raise AssertionError("secret exporter must not execute")

    _, execution, oid = ready(database)
    failure = UntrustedError("QUOTA_DENIED")

    def query(_):
        raise failure

    execution.query = query
    with pytest.raises(UntrustedError) as caught:
        execution.recover(oid)
    assert caught.value is failure
    assert diagnostic(database, oid)[0]["transport"] == {"category": "QUOTA_DENIED"}


@pytest.mark.parametrize(
    "transport",
    [
        {"category": "secret URL"},
        {"category": "QUOTA_DENIED", "msg": "secret"},
        {"category": "QUOTA_DENIED", "http_status": True},
        {"category": "QUOTA_DENIED", "exchange_code": "secret"},
    ],
)
def test_record_rejects_unbounded_transport_before_database_access(transport):
    current = {
        "exchange": "BINANCE",
        "account_id": "qa",
        "environment": "SANDBOX",
        "product": "FUTURES",
        "order_id": "qa",
        "version": 1,
        "status": "UNKNOWN",
        "exchange_order_id": None,
    }
    with pytest.raises(ValueError, match="INVALID_RECOVERY_DIAGNOSTIC"):
        record(None, current, {"outcome": "QUERY_FAILED", "transport": transport})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(secret="credential"),
        lambda p: p.update(bound_exchange_order_id=1),
        lambda p: p.update(attempts=p["attempts"] * 2),
        lambda p: p["attempts"][0].update(method="URL with token"),
        lambda p: p["attempts"][0].update(outcome="FOUND"),
        lambda p: p["attempts"][0]["transport"].update(message="secret"),
        lambda p: p["attempts"][0]["transport"].update(http_status=True),
        lambda p: p["attempts"][0]["transport"].update(exchange_code=-1007),
    ],
)
def test_diagnostic_schema_rejects_extra_or_misleading_evidence(mutation):
    value = deepcopy(proof())
    mutation(value)
    with pytest.raises(ValueError, match="INVALID_RECOVERY_QUERY_DIAGNOSTIC"):
        query_proof(value)
