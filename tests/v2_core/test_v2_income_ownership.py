import pytest
from test_v2_directional_cash import auditor
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE
from test_v2_intent_admission import database as database_fixture

from v2_core.income_ownership import income_owner
from v2_core.runtime_policy import PolicyStore

database = database_fixture
closed = closed_fixture


def row(kind="COMMISSION", value="-0.05", tid="1", when=3):
    return {
        "symbol": "BTCUSDT",
        "income_type": kind,
        "amount": value,
        "currency": "USDT",
        "occurred_at_ms": when,
        "evidence": {"source": "binance-income", "trade_id": tid},
    }


@pytest.mark.parametrize(
    "income",
    [row(), row("REALIZED_PNL", "-10", "2", 10), row("FUNDING_FEE", "0.1", "", 6)],
)
def test_owner_is_proved_from_fills(database, closed, income):
    _, episode, _ = closed
    with database() as conn:
        assert income_owner(conn, SCOPE, income) == episode


@pytest.mark.parametrize(
    "income",
    [
        row(value="-0.06"),
        row(tid="unknown"),
        row(when=1003),
        row("FUNDING_FEE", "0.1", "", 3),
        row("FUNDING_FEE", "0.1", "", 11),
        row("TRANSFER", "100", "", 5),
    ],
)
def test_unproved_cash_never_gets_assigned(database, closed, income):
    with database() as conn, pytest.raises(ValueError):
        income_owner(conn, SCOPE, income)


def test_managed_different_symbol_overlap_can_be_cash_audited(database, closed):
    from test_v2_directional_lifecycle import opening

    _, episode, income = closed
    opening(database, strategy="S8", symbol="ETHUSDT")
    PolicyStore(database, SCOPE).patch(
        {"capital.enabled": True, "sizing.pool_fraction": "0.70"},
        expected_version=0,
        reason="QA multi-symbol",
    )
    assert auditor(database, income).audit(episode)["status"] == "CASH_MATCHED"
