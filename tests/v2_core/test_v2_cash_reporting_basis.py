"""Venue cash must not count a historical theoretical bridge twice."""

import json
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from test_v2_directional_settlement import baseline, service
from test_v2_liquidation_cash import liquidation as liquidation_fixture
from test_v2_precision_settlement import rounded_episode as rounded_fixture

from services.v2_performance_reporting import performance
from tests.support.v2_admission import database as database_fixture
from v2_core.cash_adjustment_basis import (
    LEGACY_PRECISION_MODELS,
    cash_adjustment_basis_sql,
    legacy_bridge_sql,
)
from v2_core.settlement_precision import rounding_proof

database = database_fixture
rounded_episode = rounded_fixture
liquidation = liquidation_fixture


@pytest.mark.parametrize("alias", ["c; DELETE", "c.x", "c--", "C", "", "1c"])
def test_sql_contract_rejects_expressions_in_aliases(alias):
    for factory in (legacy_bridge_sql, cash_adjustment_basis_sql):
        with pytest.raises(ValueError, match="plain SQL aliases"):
            factory(adjustment=alias)
        with pytest.raises(ValueError, match="plain SQL aliases"):
            factory(intent=alias)


def test_sql_contract_is_closed_world_and_read_only():
    sql = cash_adjustment_basis_sql("cash", "intent")
    assert "ELSE 'UNKNOWN'" in sql
    assert "'THEORY_TO_VENUE_BRIDGE'" in sql
    assert "verified-liquidation-insurance-clear-v1" in sql
    assert "basis_settlement.evidence->'precision_reconciliation'" in sql
    assert all(model in sql for model in LEGACY_PRECISION_MODELS)
    assert all(word not in sql.upper() for word in ("UPDATE ", "DELETE ", "INSERT "))


def historical_rows(model=LEGACY_PRECISION_MODELS[0]):
    episode = str(uuid4())
    intent = {
        "exchange": "BINANCE",
        "account_id": "qa-report-account",
        "environment": "SANDBOX",
        "product": "FUTURES",
        "producer": "s6",
        "payload": {"symbol": "BTCUSDT"},
    }
    proof = {"model": model, "amount": "-0.00000001"}
    evidence = {
        "source": "directional-settlement-precision-v1",
        "precision": proof,
        "cash_audit_digest": "a" * 64,
        "income_digest": "b" * 64,
        "final_inventory": str(uuid4()),
    }
    adjustment = {
        "episode_id": episode,
        "adjustment_key": json.dumps(
            [intent[k] for k in ("exchange", "account_id", "environment", "product")]
            + ["CORRECTION", model + ":" + episode],
            separators=(",", ":"),
        ),
        "amount": "-0.00000001",
        "currency": "USDT",
        "kind": "CORRECTION",
        "occurred_at_ms": 10,
        "evidence": evidence,
    }
    settlement = {
        "episode_id": episode,
        "currency": "USDT",
        "evidence": {
            "source": "directional-settlement-v1",
            "precision_reconciliation": deepcopy(proof),
            "cash_audit_digest": evidence["cash_audit_digest"],
            "final_inventory": evidence["final_inventory"],
            "exchange_flat": True,
            "orders_terminal": True,
            "fills_complete": True,
            "cash_complete": True,
        },
    }
    return adjustment, intent, settlement


def classify(conn, adjustment, intent, settlement):
    # Shadow only these two tables within a read-only query. Exercise the exact
    # production predicate without fabricating live settlement or income rows.
    return conn.execute(
        f"""WITH c AS (
          SELECT * FROM jsonb_to_record(%s::jsonb) AS r(
            episode_id uuid,adjustment_key text,amount numeric,currency text,
            kind text,occurred_at_ms bigint,evidence jsonb)
        ), i AS (
          SELECT * FROM jsonb_to_record(%s::jsonb) AS r(
            exchange text,account_id text,environment text,product text,
            producer text,payload jsonb)
        ), v2_settlements AS (
          SELECT * FROM jsonb_to_recordset(%s::jsonb) AS r(
            episode_id uuid,currency text,evidence jsonb)
        ) SELECT {cash_adjustment_basis_sql()} FROM c CROSS JOIN i""",
        (json.dumps(adjustment), json.dumps(intent), json.dumps(settlement)),
    ).fetchone()[0]


@pytest.mark.parametrize("model", LEGACY_PRECISION_MODELS)
def test_sql_recognizes_every_committed_historical_bridge(database, model):
    adjustment, intent, settlement = historical_rows(model)
    with database() as conn:
        assert (
            classify(conn, adjustment, intent, [settlement]) == "THEORY_TO_VENUE_BRIDGE"
        )


@pytest.mark.parametrize(
    "defect",
    [
        "unknown_model",
        "unknown_source",
        "other_account",
        "other_episode_key",
        "wrong_kind",
        "foreign_currency",
        "live_scope",
        "wrong_producer",
        "amount_mismatch",
        "malformed_amount",
        "overflow_amount",
        "missing_income_digest",
        "bad_audit_digest",
        "invalid_inventory",
        "not_committed",
        "wrong_settlement_source",
        "wrong_settlement_episode",
        "wrong_settlement_precision",
        "wrong_settlement_audit",
        "wrong_settlement_inventory",
        "settlement_incomplete",
    ],
)
def test_unproved_precision_corrections_are_unknown_not_omitted(database, defect):
    adjustment, intent, settlement = historical_rows()
    proof = adjustment["evidence"]["precision"]
    if defect == "unknown_model":
        proof["model"] = "future-or-untrusted-model"
    elif defect == "unknown_source":
        adjustment["evidence"]["source"] = "manual"
    elif defect == "other_account":
        intent["account_id"] = "other-account"
    elif defect == "other_episode_key":
        adjustment["episode_id"] = str(uuid4())
    elif defect == "wrong_kind":
        adjustment["kind"] = "OTHER"
    elif defect == "foreign_currency":
        adjustment["currency"] = "BNB"
    elif defect == "live_scope":
        intent["environment"] = "LIVE"
    elif defect == "wrong_producer":
        intent["producer"] = "protocol-test"
    elif defect == "amount_mismatch":
        adjustment["amount"] = "-0.00000002"
    elif defect == "malformed_amount":
        proof["amount"] = "NaN"
    elif defect == "overflow_amount":
        proof["amount"] = "1e999999999"
    elif defect == "missing_income_digest":
        adjustment["evidence"].pop("income_digest")
    elif defect == "bad_audit_digest":
        adjustment["evidence"]["cash_audit_digest"] = "unverified"
    elif defect == "invalid_inventory":
        adjustment["evidence"]["final_inventory"] = "unverified"
    elif defect == "wrong_settlement_source":
        settlement["evidence"]["source"] = "manual"
    elif defect == "wrong_settlement_episode":
        settlement["episode_id"] = str(uuid4())
    elif defect == "wrong_settlement_precision":
        settlement["evidence"]["precision_reconciliation"]["amount"] = "0"
    elif defect == "wrong_settlement_audit":
        settlement["evidence"]["cash_audit_digest"] = "c" * 64
    elif defect == "wrong_settlement_inventory":
        settlement["evidence"]["final_inventory"] = str(uuid4())
    elif defect == "settlement_incomplete":
        settlement["evidence"]["cash_complete"] = False
    with database() as conn:
        assert (
            classify(
                conn,
                adjustment,
                intent,
                [] if defect == "not_committed" else [settlement],
            )
            == "UNKNOWN"
        )


def save_historical_bridge(runtime, episode, *, with_funding=False):
    """Create immutable legacy facts using their historical domain contract.

    Do not invoke the new settlement stage: it intentionally no longer creates
    precision corrections. This fixture reproduces the old persisted shape.
    """
    ledger = runtime.data.ledger
    trace = runtime.data.trace(episode)
    report = ledger.report(episode, settlement_currency="USDT")
    precision = rounding_proof(trace, report)
    final = str(uuid4())
    ledger.adjustment(
        episode_id=episode,
        source_id=precision["model"] + ":" + episode,
        amount_text=precision["amount"],
        currency="USDT",
        kind="CORRECTION",
        occurred_at_ms=20,
        evidence={
            "source": "directional-settlement-precision-v1",
            "precision": precision,
            "cash_audit_digest": "a" * 64,
            "income_digest": "b" * 64,
            "final_inventory": final,
        },
    )
    if with_funding:
        ledger.adjustment(
            episode_id=episode,
            source_id="qa-funding",
            amount_text="-0.01",
            currency="USDT",
            kind="FUNDING",
            occurred_at_ms=5,
            evidence={"source": "qa-funding"},
        )
    report = ledger.report(episode, settlement_currency="USDT")
    ledger.settle(
        episode,
        currency="USDT",
        evidence={
            "source": "directional-settlement-v1",
            "observed_at_ms": 70000,
            "ledger_revision": report["accounting_revision"],
            "exchange_flat": True,
            "orders_terminal": True,
            "fills_complete": True,
            "cash_complete": True,
            "precision_reconciliation": precision,
            "cash_audit_digest": "a" * 64,
            "final_inventory": final,
            "net_pnl": report["net_pnl"],
        },
    )


@pytest.mark.parametrize("with_funding", [False, True])
def test_daily_cash_does_not_double_count_legacy_bridge(
    database, rounded_episode, with_funding
):
    runtime, episode, _ = rounded_episode
    save_historical_bridge(runtime, episode, with_funding=with_funding)
    original = deepcopy(runtime.data.trace(episode))
    expected = Decimal("-10.106" if with_funding else "-10.096")
    for _ in range(2):
        with database() as conn:
            report = performance(conn, runtime.scope.account_id)
        cash = report["daily"]["cash"][0]
        assert Decimal(cash["net_pnl"]) == expected
        assert cash["unvalued_events"] == cash["unclassified_adjustments"] == 0
        assert Decimal(report["daily"]["closed_trades"][0]["net_pnl"]) == expected
    assert runtime.data.trace(episode) == original


@pytest.mark.parametrize(
    "source", ["manual-unknown-correction", "directional-settlement-precision-v1"]
)
def test_unclassified_correction_makes_cash_unknown_not_zero(
    database, rounded_episode, source
):
    runtime, episode, _ = rounded_episode
    runtime.data.ledger.adjustment(
        episode_id=episode,
        source_id="unverified",
        amount_text="-0.00000001",
        currency="USDT",
        kind="CORRECTION",
        occurred_at_ms=20,
        evidence={"source": source},
    )
    with database() as conn:
        cash = performance(conn, runtime.scope.account_id)["daily"]["cash"][0]
    assert cash["net_pnl"] is None
    assert cash["unvalued_events"] == cash["unclassified_adjustments"] == 1


def test_verified_insurance_clear_is_real_cash_not_a_precision_bridge(
    database, liquidation
):
    runtime, episode, income = liquidation
    baseline(database, runtime, episode)
    assert (
        service(database, income, balance="989.05046733").run_once()["status"]
        == "CLEAR"
    )
    with database() as conn:
        cash = performance(conn, runtime.scope.account_id)["daily"]["cash"][0]
    assert Decimal(cash["net_pnl"]) == Decimal("-10.94953267")
    assert cash["unvalued_events"] == cash["unclassified_adjustments"] == 0


def test_non_usdt_adjustment_stays_unvalued(database, rounded_episode):
    runtime, episode, _ = rounded_episode
    runtime.data.ledger.adjustment(
        episode_id=episode,
        source_id="qa-foreign-funding",
        amount_text="-0.01",
        currency="BNB",
        kind="FUNDING",
        occurred_at_ms=5,
        evidence={"source": "qa"},
    )
    with database() as conn:
        cash = performance(conn, runtime.scope.account_id)["daily"]["cash"][0]
    assert cash["net_pnl"] is None
    assert cash["unvalued_events"] == 1
    assert cash["unclassified_adjustments"] == 0
