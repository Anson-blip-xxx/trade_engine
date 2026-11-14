"""Versioned income audit selects cash facts without precision exceptions."""

from copy import deepcopy
from decimal import Decimal

import pytest
from test_v2_precision_contract import SCOPE, cash_case

import services.v2_directional_cash as cash_module
from v2_core.evidence import canonical
from v2_core.runtime_policy import PolicySnapshot, resolve


def snapshot(version=7, **changes):
    return PolicySnapshot(
        version, canonical(resolve({"settlement.cash_basis_version": 2, **changes}))
    )


def reconcile(trace, report, rows, **kwargs):
    return cash_module.reconcile_cash(
        trace, report, rows, scope=SCOPE, policy_snapshot=snapshot(), **kwargs
    )


def boundary():
    return cash_case("BUY", [("2997", ".03843")], [("2997", ".03844", ".02996999")])


def test_cash_basis_v2_is_explicit_and_legacy_is_default():
    assert resolve()["settlement.cash_basis_version"] == 1
    trace, report, rows = boundary()
    with pytest.raises(ValueError, match="UNEXPLAINED_FILL_PNL_PRECISION"):
        cash_module.reconcile_cash(trace, report, rows, scope=SCOPE)
    with pytest.raises(ValueError, match="UNEXPLAINED_FILL_PNL_PRECISION"):
        cash_module.reconcile_cash(
            trace,
            report,
            rows,
            scope=SCOPE,
            policy_snapshot=PolicySnapshot(7, canonical(resolve())),
        )
    assert reconcile(trace, report, rows)["status"] == "CASH_MATCHED"


def test_v2_bypasses_rounding_and_witness_with_bound_policy_provenance(monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError(
            "V2 must not explain actual cash through the old rounding model"
        )

    monkeypatch.setattr(cash_module, "rounding_proof", never)
    monkeypatch.setattr(cash_module, "_entry_observations", never)
    trace, report, rows = boundary()
    before = deepcopy((trace, report, rows))
    result = reconcile(trace, report, rows, entry_observations={"invalid": object()})
    assert result["cash_basis_version"] == 2
    assert "precision_reconciliation" not in result
    assert result["settlement_authorized"] is False
    assert result["venue_cash_analysis"]["settlement_authorized"] is False
    assert result["venue_cash_analysis"]["anomaly"]["status"] == "WITHIN_POLICY"
    assert result["cash_policy_snapshot"] == {
        "version": 7,
        "digest": snapshot().digest,
        "values": {
            "settlement.cash_basis_version": 2,
            "settlement.max_difference_per_fill_usdt": "0.000001",
            "settlement.max_difference_episode_usdt": "0.0001",
        },
    }
    assert Decimal(result["gross_pnl"]) == Decimal(".02996999")
    assert Decimal(result["provisional_net_pnl"]) == Decimal(".02976999")
    assert (trace, report, rows) == before


def test_no_fake_numeric18_correction_is_required():
    trace, report, rows = cash_case(
        "BUY", [("1e-18", "1")], [("1e-18", "1.000000000000000001", "0")]
    )
    result = reconcile(trace, report, rows)
    assert result["venue_cash_analysis"]["cash_difference"]["denominator"] == str(
        10**36
    )
    assert "precision_reconciliation" not in result
    assert Decimal(result["gross_pnl"]) == 0


def test_huge_same_source_discrepancy_remains_review_required_not_settlement_permission():
    result = reconcile(*cash_case("BUY", [("1", "1")], [("1", "1", "1000")]))
    assert result["status"] == "CASH_MATCHED"
    assert result["settlement_authorized"] is False
    analysis = result["venue_cash_analysis"]
    assert analysis["anomaly"]["status"] == "REVIEW_REQUIRED"
    assert analysis["actual_gross_pnl"] == "1000"
    assert analysis["theoretical_gross_pnl"] == "0"
    assert analysis["wallet_verified"] is False


@pytest.mark.parametrize(
    "defect",
    [
        "missing_income",
        "wrong_trade_id",
        "wrong_fee",
        "wrong_source",
        "wrong_currency",
        "report_fee",
        "wrong_report",
        "wrong_scope",
        "open_nonzero",
    ],
)
def test_v2_does_not_relax_identity_income_fees_or_economics_checks(defect):
    trace, report, rows = boundary()
    if defect == "missing_income":
        rows.pop()
    elif defect == "wrong_trade_id":
        rows[-1]["evidence"]["trade_id"] = "other"
    elif defect == "wrong_fee":
        rows[0]["amount"] = "-.00010001"
    elif defect == "wrong_source":
        rows[-1]["evidence"]["source"] = "estimate"
    elif defect == "wrong_currency":
        rows[-1]["currency"] = "USDC"
    elif defect == "report_fee":
        report["fees"] = "0"
    elif defect == "wrong_report":
        report["gross_pnl"] = "1"
    elif defect == "wrong_scope":
        trace["scope"]["account_id"] = "another"
    elif defect == "open_nonzero":
        trace["fills"][0]["evidence"]["venue_realized_pnl"] = ".00000001"
        rows[1]["amount"] = ".00000001"
    with pytest.raises(ValueError):
        reconcile(trace, report, rows)


def test_income_mismatch_is_rejected_before_cash_analysis(monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("the analyzer must follow exact income matching")

    monkeypatch.setattr(cash_module, "analyze_venue_cash", never)
    trace, report, rows = boundary()
    rows.pop()
    with pytest.raises(ValueError, match="INCOME_FILL_MISMATCH"):
        reconcile(trace, report, rows)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        True,
        PolicySnapshot(-1, canonical(resolve())),
        PolicySnapshot(True, canonical(resolve())),
        PolicySnapshot(1, canonical({"settlement.cash_basis_version": 2})),
    ],
)
def test_policy_snapshot_is_full_validated_and_versioned(bad):
    with pytest.raises(ValueError, match="INVALID_CASH_POLICY_SNAPSHOT"):
        cash_module.reconcile_cash(*boundary(), scope=SCOPE, policy_snapshot=bad)


@pytest.mark.parametrize(
    "changes",
    [
        {"settlement.cash_basis_version": 0},
        {"settlement.cash_basis_version": 3},
        {"settlement.cash_basis_version": True},
        {"settlement.cash_basis_version": "2"},
        {"settlement.max_difference_per_fill_usdt": "-.0001"},
        {"settlement.max_difference_per_fill_usdt": "0.010000000000000001"},
        {"settlement.max_difference_per_fill_usdt": 0.000001},
        {"settlement.max_difference_episode_usdt": "-.0001"},
        {"settlement.max_difference_episode_usdt": "0.010000000000000001"},
        {"settlement.max_difference_episode_usdt": "NaN"},
        {"settlement.max_difference_episode_usdt": "Infinity"},
    ],
)
def test_runtime_cash_policy_has_exact_types_and_conservative_bounds(changes):
    with pytest.raises(ValueError):
        resolve(changes)


def test_zero_review_limit_is_allowed_and_does_not_discard_cash():
    current = snapshot(
        **{
            "settlement.max_difference_per_fill_usdt": "0",
            "settlement.max_difference_episode_usdt": "0",
        }
    )
    result = cash_module.reconcile_cash(
        *boundary(), scope=SCOPE, policy_snapshot=current
    )
    assert result["venue_cash_analysis"]["anomaly"]["status"] == "REVIEW_REQUIRED"
    assert Decimal(result["gross_pnl"]) == Decimal(".02996999")
    assert result["settlement_authorized"] is False
