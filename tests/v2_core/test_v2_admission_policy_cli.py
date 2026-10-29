"""Quota allocation is isolated from old policy readers and trading controls."""

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_shared_market import shared as shared_fixture

from services.v2_admission_budget_policy import main
from v2_core.shared_market import MarketPolicy
from v2_core.state import BusinessState, StateKey

database = database_fixture
shared = shared_fixture


def test_read_defaults_and_cas_update_do_not_change_shared_market_policy(shared):
    before = MarketPolicy.read(shared, "SANDBOX").values()
    assert main([], connect=shared)["version"] == 0
    args = [
        "--set-json",
        '{"admission_reserved_weight":120,"market_reserved_weight":100}',
        "--expected-version",
        "0",
    ]
    changed = main(args, connect=shared)
    assert changed["version"] == 1
    assert changed["market_lane_limit"] == before["market_weight_limit"] - 120
    assert changed["admission_lane_limit"] == before["market_weight_limit"] - 100
    assert (
        changed["management_reserved_weight"]
        == before["shared_weight_limit"] - before["market_weight_limit"]
    )
    assert MarketPolicy.read(shared, "SANDBOX").values() == before
    with pytest.raises(ValueError, match="ADMISSION_POLICY_VERSION_CONFLICT"):
        main(args, connect=shared)
    assert main([], connect=shared)["version"] == 1
    with shared() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (0,)


def test_invalid_allocation_cannot_be_written(shared):
    store = BusinessState(shared)
    key = StateKey(
        "BINANCE",
        "market-data",
        "SANDBOX",
        "FUTURES",
        "shared-market-policy",
        "current",
    )
    store.change(
        key,
        expected_version=0,
        request_key="qa-cli-market",
        reason="QA",
        payload=MarketPolicy(
            shared_weight_limit=2000, market_weight_limit=550
        ).values(),
    )
    for value in ("551", "501", "-1", "true", '"120"'):
        with pytest.raises(ValueError):
            main(
                [
                    "--set-json",
                    '{"admission_reserved_weight":' + value + "}",
                    "--expected-version",
                    "0",
                ],
                connect=shared,
            )
    assert main([], connect=shared)["version"] == 0


def test_edits_require_cas_version_before_connecting():
    with pytest.raises(SystemExit):
        main(["--set-json", "{}"], connect=lambda: pytest.fail("no connection"))
