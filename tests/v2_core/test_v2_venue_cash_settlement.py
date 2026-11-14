"""New cash basis end-to-end with isolated journal, income and wallet facts."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE
from test_v2_directional_settlement import baseline, service
from test_v2_reported_boundary_settlement import BALANCE
from test_v2_reported_boundary_settlement import boundary_closed as boundary_fixture

from tests.support.v2_admission import database as database_fixture
from v2_core.ledger import Ledger
from v2_core.runtime_policy import PolicyStore

database = database_fixture
boundary_closed = boundary_fixture
closed = closed_fixture


def activate(database, **changes):
    store = PolicyStore(database, SCOPE)
    return store.patch(
        {"settlement.cash_basis_version": 2, **changes},
        expected_version=store.read().version,
        reason="QA cash basis",
    )


def assert_valued(runtime, episode, original):
    trace = runtime.data.trace(episode)
    assert trace["fills"] == original
    assert trace["cash"] == []
    assert len(trace["settlements"]) == 1
    report = runtime.data.ledger.report(episode, settlement_currency="USDT")
    assert report["accounting_status"] == "SETTLED"
    assert report["pnl_basis"] == "venue-cash-basis-v1"
    assert Decimal(report["gross_pnl"]) == Decimal(".00999999")
    assert Decimal(report["theoretical_gross_pnl"]) == Decimal(".01")
    assert Decimal(report["net_pnl"]) == Decimal("-.08600001")
    proof = trace["settlements"][0]["evidence"]
    assert "precision_reconciliation" not in proof
    assert proof["net_pnl"] == proof["cash_basis"]["net_pnl"]
    assert Decimal(proof["attributed_wallet_delta"]) == Decimal(report["net_pnl"])
    return report


def test_no_rounding_or_witness_and_retries(database, boundary_closed, monkeypatch):
    runtime, episode, income = boundary_closed
    activate(database)
    original = deepcopy(runtime.data.trace(episode)["fills"])

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy rounding proof must not run")

    monkeypatch.setattr("services.v2_directional_cash.rounding_proof", forbidden)
    for _ in range(3):
        assert (
            service(database, income, balance=BALANCE).run_once()["status"] == "CLEAR"
        )
        assert_valued(runtime, episode, original)


@pytest.mark.parametrize(
    "defect", ["wallet", "missing_income", "trade_id", "fee", "duplicate", "review"]
)
def test_real_inconsistency_still_blocks(database, boundary_closed, defect):
    runtime, episode, income = boundary_closed
    changes = (
        {"settlement.max_difference_per_fill_usdt": "0"} if defect == "review" else {}
    )
    activate(database, **changes)
    balance = BALANCE
    if defect == "wallet":
        balance = "999.913999990000000001"
    elif defect == "missing_income":
        income.rows.pop()
    elif defect == "trade_id":
        income.rows[-1]["tradeId"] = "wrong"
    elif defect == "fee":
        income.rows[0]["income"] = "-.05000001"
    elif defect == "duplicate":
        extra = deepcopy(income.rows[-1])
        extra["tranId"] = 999
        income.rows.append(extra)
    result = service(database, income, balance=balance).run_once()
    assert result["status"] == "BLOCKED"
    if defect == "wallet":
        assert result["settlements"][episode]["error_code"] == "WALLET_CASH_MISMATCH"
    if defect == "review":
        assert (
            result["settlements"][episode]["error_code"] == "VENUE_CASH_REVIEW_REQUIRED"
        )
    trace = runtime.data.trace(episode)
    assert trace["cash"] == trace["settlements"] == []


def test_basis_commit_failure_rolls_back_and_retries(
    database, boundary_closed, monkeypatch
):
    runtime, episode, income = boundary_closed
    activate(database)
    original = deepcopy(runtime.data.trace(episode)["fills"])
    original_settle = Ledger.settle

    def crash(self, *args, **kwargs):
        original_settle(self, *args, **kwargs)
        raise RuntimeError("QA rollback after basis insert")

    monkeypatch.setattr(Ledger, "settle", crash)
    assert service(database, income, balance=BALANCE).run_once()["status"] == "BLOCKED"
    trace = runtime.data.trace(episode)
    assert trace["cash"] == trace["settlements"] == []
    monkeypatch.setattr(Ledger, "settle", original_settle)
    assert service(database, income, balance=BALANCE).run_once()["status"] == "CLEAR"
    assert_valued(runtime, episode, original)


def test_concurrent_basis_once(database, boundary_closed):
    runtime, episode, income = boundary_closed
    activate(database)
    original = deepcopy(runtime.data.trace(episode)["fills"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: service(database, income, balance=BALANCE).run_once(),
                range(2),
            )
        )
    assert any(r["status"] == "CLEAR" for r in results)
    assert service(database, income, balance=BALANCE).run_once()["status"] == "CLEAR"
    assert_valued(runtime, episode, original)


def test_real_funding_not_precision_correction(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    activate(database)
    result = service(database, income).run_once()
    assert result["status"] == "CLEAR"
    trace = runtime.data.trace(episode)
    assert len(trace["cash"]) == 1
    assert trace["cash"][0]["kind"] == "FUNDING"
    report = runtime.data.ledger.report(episode, settlement_currency="USDT")
    assert report["pnl_basis"] == "venue-cash-basis-v1"
    assert Decimal(report["net_pnl"]) == Decimal("-10.106")


def test_policy_change_fences_final_commit(database, boundary_closed, monkeypatch):
    runtime, episode, income = boundary_closed
    activate(database)
    stage = service(database, income, balance=BALANCE)
    original_external = stage._external_cash

    def changed(**kwargs):
        result = original_external(**kwargs)
        read = PolicyStore.read
        monkeypatch.setattr(
            PolicyStore, "read", lambda self: replace(read(self), version=999)
        )
        return result

    monkeypatch.setattr(stage, "_external_cash", changed)
    result = stage.run_once()
    assert result["status"] == "BLOCKED"
    assert (
        result["settlements"][episode]["error_code"] == "SETTLEMENT_POLICY_SUPERSEDED"
    )
    assert runtime.data.trace(episode)["settlements"] == []


@pytest.mark.parametrize(
    "field",
    [
        "net_pnl",
        "attributed_wallet_delta",
        "income_digest",
        "account_scope",
        "analysis_status",
        "analysis_fill_pnl",
        "analysis_sum_abs",
        "analysis_limits",
        "analysis_model",
        "policy_version",
        "policy_digest",
        "policy_values",
    ],
)
def test_ledger_independently_fences_basis_proof(
    database, boundary_closed, monkeypatch, field
):
    runtime, episode, income = boundary_closed
    activate(database)
    original_settle = Ledger.settle

    def changed(self, eid, *, currency, evidence):
        evidence = deepcopy(evidence)
        analysis = evidence["cash_basis"]["analysis"]
        if field == "analysis_status":
            analysis["anomaly"]["status"] = "EXACT"
        elif field == "analysis_fill_pnl":
            analysis["fills"][0]["actual_pnl"] = "999"
        elif field == "analysis_sum_abs":
            analysis["difference_summary"]["absolute_sum"] = {
                "numerator": "0",
                "denominator": "1",
                "decimal": "0",
            }
        elif field == "analysis_limits":
            analysis["anomaly"]["policy"]["max_abs_per_fill"] = "0.01"
        elif field == "analysis_model":
            analysis["model"] = "unverified-cash-v1"
        elif field == "policy_version":
            evidence["cash_policy_snapshot"]["version"] = 999
        elif field == "policy_digest":
            evidence["cash_policy_snapshot"]["digest"] = "0" * 64
        elif field == "policy_values":
            evidence["cash_policy_snapshot"]["values"][
                "settlement.max_difference_per_fill_usdt"
            ] = "0.01"
        else:
            evidence[field] = {} if field == "account_scope" else "0"
        return original_settle(self, eid, currency=currency, evidence=evidence)

    monkeypatch.setattr(Ledger, "settle", changed)
    result = service(database, income, balance=BALANCE).run_once()
    assert result["status"] == "BLOCKED"
    if field.startswith("analysis_"):
        assert (
            result["settlements"][episode]["error_code"]
            == "VENUE_CASH_ANALYSIS_MISMATCH"
        )
    elif field.startswith("policy_"):
        assert (
            result["settlements"][episode]["error_code"]
            == "VENUE_CASH_POLICY_SNAPSHOT_MISMATCH"
        )
    assert runtime.data.trace(episode)["settlements"] == []
