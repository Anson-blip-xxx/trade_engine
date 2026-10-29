"""Research-only comparisons cannot become an execution path or PnL forecast."""

import time
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE
from test_v2_directional_settlement import baseline, service
from test_v2_intent_admission import database as database_fixture
from test_v2_strategy_console import case, setup
from test_v2_strategy_console import console_case as console_fixture

from services.v2_trade_review import build_review, comparisons, trade_review

database = database_fixture
closed = closed_fixture
console_case = console_fixture
__all__ = ["case", "setup"]


def trade(identity="a", **changes):
    return {
        "id": identity,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "source": "s3",
        "decided_ms": 1000,
        "opened_ms": 1010,
        "closed_ms": 2000,
        "close_known_ms": 2100,
        "entry_mode": "RIGHT_MOMENTUM",
        "score": 90,
        "net_pnl": "5",
        "maintenance": False,
        **changes,
    }


def report(rows):
    return build_review(rows, start_ms=0, now_ms=1000000)


def test_cooldown_uses_only_close_known_at_candidate_time():
    a = trade()
    b = trade("b", decided_ms=2050, opened_ms=2060, closed_ms=3000, close_known_ms=3100)
    c = trade(
        "c",
        decided_ms=3200,
        opened_ms=3300,
        closed_ms=4000,
        close_known_ms=4100,
        net_pnl="-4",
    )
    result = report([c, a, b])
    assert result["trades"][1]["prior_close_gap_ms"] is None  # a not yet known closed
    assert result["trades"][2]["prior_episode_id"] == "b"
    variant = result["comparisons"][0]
    assert variant["excluded_count"] == 1 and variant["unknown_count"] == 2
    assert variant["excluded_actual"]["net_pnl"] == "-4"
    assert variant["simulated_pnl"] is None


def test_outcome_labels_never_change_filter_choices():
    rows = [trade(), trade("b", decided_ms=3000, opened_ms=3010, closed_ms=4000)]
    first = report(rows)
    altered = deepcopy(rows)
    for r in altered:
        r["net_pnl"] = "-999"
    second = report(altered)
    assert [v["decisions"] for v in first["comparisons"]] == [
        v["decisions"] for v in second["comparisons"]
    ]


def test_takeover_threshold_unknowns_maintenance_and_exact_money():
    result = report(
        [
            trade(
                "a",
                entry_mode="S6B_TREND_TAKEOVER",
                score=59,
                net_pnl="-0.123456789123456789",
            ),
            trade("b", entry_mode="S6B_TREND_TAKEOVER", score=60),
            trade("c", entry_mode=None),
            trade("d", maintenance=True, net_pnl="9999"),
        ]
    )
    variant = result["comparisons"][3]
    assert (
        variant["kept_count"],
        variant["excluded_count"],
        variant["unknown_count"],
    ) == (1, 1, 1)
    assert variant["excluded_actual"]["net_pnl"] == "-0.123456789123456789"
    assert result["summary"]["maintenance_excluded"] == 1
    assert (
        not result["execution_authorized"] and not result["automatic_parameter_updates"]
    )


def test_open_trade_is_not_counted_as_settled_even_if_input_has_net():
    result = report([trade(closed_ms=None, close_known_ms=None)])
    assert result["summary"]["settled_trades"] == 0
    assert result["summary"]["unsettled_or_unvalued"] == 1
    assert result["trades"][0]["held_minutes"] is None


def test_same_symbol_context_does_not_cross_symbols_and_window():
    result = build_review(
        [
            trade(),
            trade(
                "b", symbol="ETHUSDT", decided_ms=3000, opened_ms=3010, closed_ms=4000
            ),
        ],
        start_ms=2500,
        now_ms=5000,
        truncated=True,
    )
    assert (
        len(result["trades"]) == 1 and result["trades"][0]["prior_episode_id"] is None
    )
    assert result["truncated"] is True


@pytest.mark.parametrize(
    "options",
    [
        {"cooldowns": ()},
        {"cooldowns": (0,)},
        {"cooldowns": (True,)},
        {"cooldowns": (1, 1)},
        {"takeover_scores": (101,)},
        {"takeover_scores": ("60",)},
    ],
)
def test_bounded_shadow_hypotheses(options):
    with pytest.raises(ValueError):
        comparisons([], **options)


def test_real_settlement_review_is_scoped_read_only_and_exact(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    assert service(database, income).run_once()["status"] == "CLEAR"
    with database() as c:
        c.execute("SET TRANSACTION READ ONLY")
        before = c.execute(
            "SELECT (SELECT count(*) FROM v2_orders),(SELECT count(*) FROM v2_state_history)"
        ).fetchone()
        result = trade_review(c, SCOPE)
        other = trade_review(c, replace(SCOPE, account_id="foreign"))
        after = c.execute(
            "SELECT (SELECT count(*) FROM v2_orders),(SELECT count(*) FROM v2_state_history)"
        ).fetchone()
    assert before == after and other["trades"] == []
    assert result["summary"]["settled_trades"] == 1
    row = result["trades"][0]
    assert row["id"] == episode and Decimal(row["net_pnl"]) == Decimal("-10.106")
    assert Decimal(row["fees_usdt"]) == Decimal("0.096")
    assert (
        row["mfe"]
        is row["followup_15_return_pct"]
        is row["followup_60_return_pct"]
        is None
    )


def test_unsettled_and_future_fills_are_not_invented(database, closed):
    with database() as c:
        c.execute("SET TRANSACTION READ ONLY")
        result = trade_review(c, SCOPE)
        assert result["summary"]["settled_trades"] == 0
        assert result["summary"]["unsettled_or_unvalued"] == 1
        assert trade_review(c, SCOPE, now_ms=1)["trades"] == []


def test_read_write_connection_is_rejected(database):
    with database() as c, pytest.raises(ValueError, match="READ_ONLY"):
        trade_review(c, SCOPE)


@pytest.mark.parametrize("days", [0, True, "14", 365])
def test_report_window_bounds(database, days):
    with database() as c, pytest.raises(ValueError, match="WINDOW"):
        trade_review(c, SCOPE, days=days)


def test_console_cache_reauthorizes_and_is_detached(console_case):
    console, _, accounts = console_case
    registry = accounts[4]
    first = console.review(registry)
    second = console.review(registry)
    assert not first["cached"] and second["cached"]
    second["limitations"].append("caller mutation")
    assert "caller mutation" not in console.review(registry)["limitations"]
    assert console.inspect(registry)["version"] == 1
    assert console.review(accounts[5])["cached"] is False
    console._review_slot.acquire()
    try:
        assert console.review(registry)["status"] == "BUSY"
    finally:
        console._review_slot.release()
    console._review_cache = {
        key: (time.monotonic() - 61, data)
        for key, (_, data) in console._review_cache.items()
    }
    from collections import OrderedDict

    console._review_cache = OrderedDict(console._review_cache)
    assert console.review(registry)["cached"] is False


def test_cached_report_is_not_available_to_retired_or_foreign_account(console_case):
    console, _, accounts = console_case
    registry = accounts[4]
    console.review(registry)
    with console.connect() as c:
        c.execute(
            "INSERT INTO v2_account_retirements(tenant_id,registry_id,reason) VALUES (%s,%s,'USER_REMOVED_INVALID_CREDENTIAL')",
            (console.tenant, registry),
        )
    for target in (registry, accounts[6]):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            console.review(target)


def test_review_timeout_releases_slot_without_caching_partial_report(
    console_case, monkeypatch
):
    from psycopg.errors import QueryCanceled

    import services.v2_trade_review as module

    console, _, accounts = console_case
    original = module.trade_review

    def timeout(conn, *args, **kwargs):
        conn.execute("SET LOCAL statement_timeout='1ms'")
        conn.execute("SELECT pg_sleep(0.02)")

    monkeypatch.setattr(module, "trade_review", timeout)
    with pytest.raises(QueryCanceled):
        console.review(accounts[4])
    assert not console._review_cache
    monkeypatch.setattr(module, "trade_review", original)
    assert console.review(accounts[4])["status"] == "AVAILABLE"


def test_context_before_window_and_cooldown_boundary():
    prior = trade(closed_ms=2000, close_known_ms=2100)
    candidate = trade(
        "b",
        decided_ms=302000,
        opened_ms=302100,
        closed_ms=303000,
        close_known_ms=303100,
    )
    result = build_review([prior, candidate], start_ms=300000, now_ms=400000)
    assert result["comparisons"][0]["kept_count"] == 1  # exactly five minutes
    assert result["comparisons"][1]["excluded_count"] == 1
    assert result["trades"][0]["prior_episode_id"] == "a"
