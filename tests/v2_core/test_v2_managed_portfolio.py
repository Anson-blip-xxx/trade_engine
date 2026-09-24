import pytest
from test_v2_account_coverage import covered as covered_fixture
from test_v2_account_coverage import inventory
from test_v2_intent_admission import database as database_fixture
from test_v2_protection_child import SCOPE
from test_v2_protection_child import setup as setup_fixture

from v2_core.managed_portfolio import ManagedPortfolio

database = database_fixture
setup = setup_fixture
covered = covered_fixture


def test_other_owned_protected_symbol_does_not_block_new_symbol(database, covered):
    _, reader, _ = covered
    portfolio = ManagedPortfolio(database, reader, scope=SCOPE, clock_ms=lambda: 1000)
    before = portfolio.facts()
    assert portfolio.blockers(inventory(reader), "ETHUSDT", before=before) == []
    assert "EXISTING_SYMBOL_POSITION" in portfolio.blockers(
        inventory(reader), "BTCUSDT"
    )


@pytest.mark.parametrize("failure", ["naked", "unowned", "changed"])
def test_multisymbol_does_not_bypass_safety(database, covered, failure):
    _, reader, _ = covered
    portfolio = ManagedPortfolio(database, reader, scope=SCOPE, clock_ms=lambda: 1000)
    before = portfolio.facts()
    if failure == "naked":
        reader.rows["/fapi/v1/openAlgoOrders"] = []
    elif failure == "unowned":
        reader.rows["/fapi/v1/openOrders"] = [{"symbol": "ETHUSDT", "orderId": 999}]
    else:
        before["episodes"] = []
    assert portfolio.blockers(inventory(reader), "ETHUSDT", before=before)
