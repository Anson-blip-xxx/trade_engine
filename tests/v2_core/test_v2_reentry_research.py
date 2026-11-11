from copy import deepcopy

import pytest
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE
from test_v2_reentry import bar

from services.v2_reentry_research import review_reentries
from tests.support.v2_admission import database as database_fixture

database = database_fixture
closed = closed_fixture


def test_readonly_actual_sql_and_account_scope(database, closed):
    calls = []
    with database() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        result = review_reentries(
            conn, SCOPE, read_bars=lambda *args: calls.append(args)
        )
    assert result["rows"] == [] and not calls
    assert not result["execution_authorized"] and result["simulated_pnl"] is None


def test_readwrite_is_rejected(database):
    with database() as conn, pytest.raises(ValueError, match="READ_ONLY"):
        review_reentries(conn, SCOPE, read_bars=lambda *args: [])


def test_reconstruction_keeps_outcomes_out_of_assessment(monkeypatch):
    prev = {
        "id": "a",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "maintenance": False,
        "prior_episode_id": None,
        "closed_ms": 25 * 60000,
        "close_known_ms": 25 * 60000,
        "exit_reason": "PEAK_DRAWDOWN",
        "net_pnl": "10",
    }
    current = {
        "id": "b",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "maintenance": False,
        "prior_episode_id": "a",
        "decided_ms": 26 * 60000,
        "entry_mode": "RIGHT_MOMENTUM",
        "net_pnl": "-10",
    }
    source = {
        "trades": [prev, current],
        "truncated": False,
        "as_of": "2026-10-01T00:00:00+08:00",
    }
    monkeypatch.setattr(
        "services.v2_reentry_research.trade_review", lambda *a, **k: deepcopy(source)
    )

    class Connection:
        def execute(self, query, params):
            assert params[-4:] == (
                SCOPE.exchange,
                SCOPE.account_id,
                SCOPE.environment,
                SCOPE.product,
            )
            return self

        def fetchall(self):
            return [
                ("a", 24 * 60000, "source-wave", "100"),
                ("b", 26 * 60000, "source-wave", None),
            ]

    def read(target, start, end):
        assert target == "BTCUSDT" and end == 26 * 60000
        return list(
            reversed(
                [
                    bar(i).payload()
                    if i < 25
                    else bar(i, h="102", c="101.5", v="20").payload()
                    for i in range(start // 60000, end // 60000)
                ]
            )
        )

    first = review_reentries(Connection(), SCOPE, read_bars=read, now_ms=30 * 60000)
    source["trades"][1]["net_pnl"] = "99999"
    second = review_reentries(Connection(), SCOPE, read_bars=read, now_ms=30 * 60000)
    assert first["rows"][0]["assessment"] == second["rows"][0]["assessment"]
    assert first["counts"] == {"READY": 1}
    assert first["rows"][0]["same_producer_episode"]
    missing = review_reentries(
        Connection(), SCOPE, read_bars=lambda *a: [], now_ms=30 * 60000
    )
    assert missing["counts"] == {"UNKNOWN": 1}


@pytest.mark.parametrize("limit", [0, 31, True])
def test_reconstruction_bound(limit):
    with pytest.raises(ValueError, match="BOUND"):
        review_reentries(None, SCOPE, read_bars=lambda *a: [], max_pairs=limit)


def test_account_selection_cannot_silently_pick_another_environment():
    from services.v2_reentry_research_entry import select_scope

    class Connection:
        def __init__(self, rows):
            self.rows = rows

        def execute(self, *args):
            return self

        def fetchall(self):
            return self.rows

    for rows in (
        [],
        [("BINANCE", "account", "LIVE", "FUTURES")],
        [("BINANCE", "a", "SANDBOX", "FUTURES")] * 2,
    ):
        with pytest.raises(ValueError):
            select_scope(Connection(rows), active_sandbox=True)
    with pytest.raises(ValueError, match="EXPLICIT"):
        select_scope(None)
    assert (
        select_scope(
            Connection([("BINANCE", "a", "SANDBOX", "FUTURES")]), active_sandbox=True
        ).environment
        == "SANDBOX"
    )
