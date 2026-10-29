"""Pipeline routing contract; concrete proof checks live in settlement-entry QA."""

from copy import deepcopy

import pytest
from test_v2_trading_pipeline import case, database  # noqa: F401


def proof():
    return {
        "status": "ENTRY_CLEAR_PENDING_INCOME",
        "risk_reservations_retained": True,
        "execution_authorized": False,
    }


def configure(pipeline, returned):
    original = {"status": "BLOCKED", "reason": "WAITING_INCOME_LAG"}
    seen = []
    pipeline.settlement.run_once = lambda: deepcopy(original)

    def clearance(result, coverage):
        seen.append((result, coverage))
        return returned

    pipeline.settlement.entry_clearance = clearance
    return original, seen


def test_pending_income_diagnostic_survives_entry_clearance(case, database):  # noqa: F811
    pipeline, _, _, _, _ = case
    original, seen = configure(pipeline, proof())
    result = pipeline.run_once()
    assert result["status"] == "CYCLE_COMPLETE", result
    assert result["phases"]["settlement"] == original
    assert result["phases"]["settlement_entry_clearance"] == proof()
    assert len(seen) == 1
    assert "s6" in result["phases"]
    # Only normal prepare is reached; neither this proof nor the test dispatches.
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_orders WHERE status='PREPARED'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT count(*) FROM v2_fills").fetchone()[0] == 0


@pytest.mark.parametrize(
    "returned",
    [
        None,
        {},
        {"status": "CLEAR"},
        {**proof(), "risk_reservations_retained": False},
        {**proof(), "risk_reservations_retained": 1},
        {**proof(), "execution_authorized": True},
        {**proof(), "error_code": "UNKNOWN_PROOF"},
        {**proof(), "evidence": {"status": "UNKNOWN"}},
    ],
)
def test_bad_clearance_keeps_entry_blocked(case, database, returned):  # noqa: F811
    pipeline, _, _, _, _ = case
    configure(pipeline, returned)
    result = pipeline.run_once()
    assert result["status"] == "ENTRY_BLOCKED"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0


@pytest.mark.parametrize("unsafe", ["protection", "exits"])
def test_accounting_clearance_cannot_override_position_safety(case, unsafe):  # noqa: F811
    pipeline, _, _, _, _ = case
    _, seen = configure(pipeline, proof())
    getattr(pipeline, unsafe).status = "BLOCKED"
    result = pipeline.run_once()
    assert result["status"] == "ENTRY_BLOCKED"
    assert seen == []


def test_clearance_error_keeps_entry_blocked(case):  # noqa: F811
    pipeline, _, _, _, _ = case
    configure(pipeline, proof())

    def failure(*args):
        raise TimeoutError("private diagnostic")

    pipeline.settlement.entry_clearance = failure
    result = pipeline.run_once()
    assert result["status"] == "ENTRY_BLOCKED"
    assert (
        result["phases"]["settlement_entry_clearance"]["error_code"] == "TimeoutError"
    )
