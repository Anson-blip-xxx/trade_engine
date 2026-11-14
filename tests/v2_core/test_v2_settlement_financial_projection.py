"""Only committed settlement analysis may leave the financial safety gate."""

from contextlib import nullcontext
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from services import v2_settlement_entry as entry
from services.v2_directional_settlement import DirectionalSettlementStage
from services.v2_trading_pipeline import _stage_clear, failed
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical

EPISODE = "11111111-1111-4111-8111-111111111111"
LAGGING = "22222222-2222-4222-8222-222222222222"
INVENTORY = "33333333-3333-4333-8333-333333333333"


def financial_result():
    return {
        "status": "CLEAR",
        "settlements": {
            EPISODE: {
                "status": "SETTLED",
                "episode": EPISODE,
                "net_pnl": "-1.00000001",
                "final_inventory": INVENTORY,
                "settlement_authorized": False,
            }
        },
        "outcomes": {
            EPISODE: {"status": "BLOCKED", "error_code": "DIRECTIONAL_OUTCOME_CONFLICT"}
        },
        "settlement_authorized": False,
    }


def test_committed_outcome_failure_is_isolated_without_mutating_diagnostics():
    stage = object.__new__(DirectionalSettlementStage)
    result = financial_result()
    original = deepcopy(result)

    assert failed(result)
    assert _stage_clear("settlement", stage, result)
    assert result == original
    assert failed(result)
    assert not _stage_clear("protection", stage, result)
    assert not _stage_clear("settlement", SimpleNamespace(), result)


@pytest.mark.parametrize(
    ("net_pnl", "accepted"),
    [
        ("-10.106000000000000000000000000000000000", True),
        ("-1.000000000000000001000000000000000000", True),
        ("-1.0000000000000000001", False),
        ("0.0000000000000000001", False),
    ],
)
def test_settled_financial_amount_accepts_only_exact_storage_precision(net_pnl, accepted):
    stage = object.__new__(DirectionalSettlementStage)
    result = financial_result()
    result["settlements"][EPISODE]["net_pnl"] = net_pnl

    assert _stage_clear("settlement", stage, result) is accepted
    assert result["settlements"][EPISODE]["net_pnl"] == net_pnl


@pytest.mark.parametrize(
    "defect",
    [
        "top_error", "top_extra", "top_blocked", "top_permission", "missing_outcomes",
        "settlement_collection", "settlement_missing_field", "settlement_extra",
        "settlement_error", "settlement_unsettled", "settlement_identity",
        "settlement_permission", "settlement_inventory", "settlement_nan",
        "settlement_float", "outcome_collection", "outcome_identity",
        "outcome_extra", "outcome_nested_error", "outcome_unknown_status",
        "outcome_unsanitized_error", "outcome_missing_error", "outcome_not_record",
    ],
)
def test_unknown_or_financial_failure_cannot_use_analysis_exception(defect):
    stage = object.__new__(DirectionalSettlementStage)
    result = financial_result()
    settled, outcome = result["settlements"][EPISODE], result["outcomes"][EPISODE]
    if defect == "top_error":
        result["error_code"] = "UNKNOWN"
    elif defect == "top_extra":
        result["unknown"] = {}
    elif defect == "top_blocked":
        result["status"] = "BLOCKED"
    elif defect == "top_permission":
        result["settlement_authorized"] = True
    elif defect == "missing_outcomes":
        del result["outcomes"]
    elif defect == "settlement_collection":
        result["settlements"] = []
    elif defect == "settlement_missing_field":
        del settled["net_pnl"]
    elif defect == "settlement_extra":
        settled["unknown"] = {}
    elif defect == "settlement_error":
        settled["error_code"] = "WALLET_CASH_MISMATCH"
    elif defect == "settlement_unsettled":
        settled["status"] = "CALCULATED"
    elif defect == "settlement_identity":
        settled["episode"] = LAGGING
    elif defect == "settlement_permission":
        settled["settlement_authorized"] = True
    elif defect == "settlement_inventory":
        settled["final_inventory"] = "unknown"
    elif defect == "settlement_nan":
        settled["net_pnl"] = "NaN"
    elif defect == "settlement_float":
        settled["net_pnl"] = -1.0
    elif defect == "outcome_collection":
        result["outcomes"] = []
    elif defect == "outcome_identity":
        result["outcomes"] = {"unknown": outcome}
    elif defect == "outcome_extra":
        outcome["unknown"] = {}
    elif defect == "outcome_nested_error":
        outcome["cash"] = {"error_code": "WALLET_CASH_MISMATCH"}
    elif defect == "outcome_unknown_status":
        outcome["status"] = "UNKNOWN"
    elif defect == "outcome_unsanitized_error":
        outcome["error_code"] = "an arbitrary transport message"
    elif defect == "outcome_missing_error":
        del outcome["error_code"]
    else:
        result["outcomes"][EPISODE] = "BLOCKED"

    assert not _stage_clear("settlement", stage, result)


def test_empty_scan_and_successful_analysis_preserve_concrete_stage_contract():
    stage = object.__new__(DirectionalSettlementStage)
    result = financial_result()
    result["outcomes"][EPISODE] = {
        "episode_id": EPISODE,
        "closed_at_ms": 1,
        "return_pct": "-1.3",
        "quality_score": "70",
        "closing_price": "1.01",
    }
    assert _stage_clear("settlement", stage, result)
    result["settlements"] = result["outcomes"] = {}
    assert _stage_clear("settlement", stage, result)
    assert not _stage_clear("settlement", stage, {"status": "CLEAR"})


def test_legacy_simple_stages_keep_their_original_safety_checks():
    stage = SimpleNamespace()
    assert _stage_clear("settlement", stage, {"status": "CLEAR"})
    assert not _stage_clear("settlement", stage, {"status": "BLOCKED"})
    assert not _stage_clear(
        "settlement", stage, {"status": "CLEAR", "nested": {"error_code": "FAILED"}}
    )


@pytest.mark.parametrize("current_settlement", [True, False])
def test_income_grace_ignores_only_outcomes_with_current_scoped_settlement(
    monkeypatch, current_settlement
):
    stage = object.__new__(DirectionalSettlementStage)
    stage.scope = AccountScope("BINANCE", "qa-outcome-isolation", "SANDBOX", "FUTURES")
    stage.limit = 10
    stage.clock = lambda: 1000
    proof = {"observation_id": INVENTORY}
    stage.coverage = SimpleNamespace(reuse_after_protection=lambda _: proof)
    selected = SimpleNamespace(
        version=1, deleted=False, payload_json=canonical({"enabled": True})
    )
    monkeypatch.setattr(entry, "BusinessState", lambda _: SimpleNamespace(read=lambda _: selected))
    statements = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append(statement)
            if statement.startswith("SET TRANSACTION"):
                return None
            if "JOIN v2_settlements s" in statement:
                assert "s.revision=e.accounting_revision" in statement
                assert "AND e.status='SETTLED'" in statement
                assert params == (*stage.scope.__dict__.values(), [EPISODE])
                return SimpleNamespace(
                    fetchall=lambda: [(EPISODE,)] if current_settlement else []
                )
            assert "AND e.status<>'SETTLED'" in statement
            return SimpleNamespace(fetchall=lambda: [(
                LAGGING, "BTCUSDT", 1, Decimal(1), Decimal(1), 900,
                stage.scope.key, "HELD", "10", 1, False,
            )])

    stage.connect = lambda: nullcontext(Connection())
    result = financial_result()
    result["status"] = "BLOCKED"
    result["settlements"] = {
        LAGGING: {
            "status": "BLOCKED", "reason": "WAITING_INCOME_LAG",
            "settlement_authorized": False,
        }
    }

    answer = stage.entry_clearance(result, proof)

    if current_settlement:
        assert answer["status"] == "ENTRY_CLEAR_PENDING_INCOME"
        assert answer["risk_reservations_retained"]
        assert answer["execution_authorized"] is answer["settlement_authorized"] is False
        assert answer["evidence"]["episodes"][0]["episode"] == LAGGING
    else:
        assert answer is None
    assert len([s for s in statements if "JOIN v2_settlements s" in s]) == 1
