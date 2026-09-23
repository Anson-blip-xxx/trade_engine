import json
from copy import deepcopy

from market_fakes import Budget, MarketHTTP
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_outcomes import outcome as outcome_fixture
from test_v2_directional_replay import SCOPE
from test_v2_intent_admission import database as database_fixture

from services.v2_directional_followup import DirectionalFollowupStage
from v2_core.public_market import BinancePublicMarket

outcome = outcome_fixture
database = database_fixture
closed = closed_fixture


class Market:
    environment = "SANDBOX"

    def __init__(self, now):
        self.now, self.calls = now, []
        self.mutate = lambda path, value: value

    def __call__(self, path, params):
        self.calls.append((path, deepcopy(params)))
        if path == "/fapi/v1/time":
            value = {"serverTime": self.now}
        else:
            opened, closed = params["startTime"], params["endTime"]
            value = [
                [opened, "90", "91", "89", "90", "1", closed, "90", 1, "1", "90", "0"]
            ]
        return self.mutate(path, value)


def stage(outcome, now):
    journal, _, _ = outcome
    market = Market(now)
    return (
        DirectionalFollowupStage(
            journal.connect,
            market,
            scope=SCOPE,
            clock_ms=lambda: now,
        ),
        market,
    )


def test_due_outcome_uses_one_exact_closed_public_minute(outcome):
    journal, episode, result = outcome
    collector, market = stage(outcome, 3720000)
    collected = collector.run_once()
    assert collected["status"] == "CLEAR"
    assert collected["followups"][episode]["episode_id"] == episode
    assert market.calls == [
        ("/fapi/v1/time", {}),
        (
            "/fapi/v1/klines",
            {
                "symbol": "BTCUSDT",
                "interval": "1m",
                "startTime": 3660000,
                "endTime": 3719999,
                "limit": 1,
            },
        ),
    ]
    assert collector.run_once() == {"status": "CLEAR", "followups": {}}
    with journal.connect() as conn:
        evidence = conn.execute(
            "SELECT evidence FROM v2_directional_followups WHERE episode_id=%s",
            (episode,),
        ).fetchone()[0]
    assert evidence["source"] == "binance-public-closed-1m-v1"
    assert evidence["candle_close_ms"] >= result["closed_at_ms"] + 3600000


def test_not_due_does_no_io_and_open_candle_blocks_admission(outcome):
    _, _, result = outcome
    before, market = stage(outcome, result["closed_at_ms"] + 3599999)
    assert before.run_once() == {"status": "CLEAR", "followups": {}}
    assert market.calls == []
    waiting, market = stage(outcome, result["closed_at_ms"] + 3600000)
    assert waiting.run_once() == {"status": "PENDING", "followups": {}}
    assert market.calls == [("/fapi/v1/time", {})]


def test_invalid_or_stale_public_evidence_fails_closed(outcome):
    collector, market = stage(outcome, 3720000)
    market.mutate = lambda path, value: value[:-1] if path.endswith("klines") else value
    result = collector.run_once()
    assert result["status"] == "BLOCKED"
    assert next(iter(result["followups"].values())) == {
        "status": "BLOCKED",
        "error_code": "ValueError",
    }
    collector, market = stage(outcome, 3720000)
    market.now -= 5001
    try:
        collector.run_once()
    except ValueError as exc:
        assert "clock" in str(exc)
    else:
        raise AssertionError("stale exchange time accepted")


def test_public_transport_allows_only_exact_single_closed_minute():
    row = [[0, "1", "1", "1", "1", "1", 59999, "1", 1, "1", "1", "0"]]
    budget, http = Budget(), MarketHTTP(raw=json.dumps(row).encode())
    client = BinancePublicMarket(
        environment="SANDBOX",
        budget=budget,
        enabled=True,
        connection_factory=http,
    )
    params = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "startTime": 0,
        "endTime": 59999,
        "limit": 1,
    }
    assert client("/fapi/v1/klines", params) == row
    assert budget.weights == [1]
