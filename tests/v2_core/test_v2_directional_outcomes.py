from decimal import Decimal
from pathlib import Path

import pytest
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE
from test_v2_directional_settlement import baseline, service
from test_v2_intent_admission import database as database_fixture

from v2_core.directional_outcomes import DirectionalHistory, DirectionalOutcomeJournal

database = database_fixture
closed = closed_fixture


@pytest.fixture
def outcome(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    assert service(database, income).run_once()["status"] == "CLEAR"
    journal = DirectionalOutcomeJournal(database, scope=SCOPE)
    result = journal.record(episode)
    return journal, episode, result


def market_signal():
    return {"symbol": "BTCUSDT", "signal": "TREND_UP", "features": {}}


def test_directional_history_migration_is_repeatable(database):
    script = (
        Path(__file__).resolve().parents[2]
        / "db/migrations/20260923_directional_history.sql"
    ).read_text()
    with database() as conn:
        conn.execute("DROP TABLE v2_directional_followups")
        conn.execute("DROP TABLE v2_directional_outcomes")
        conn.execute(script)
        conn.execute(script)
        assert conn.execute(
            "SELECT to_regclass('v2_directional_outcomes'),to_regclass('v2_directional_followups')"
        ).fetchone() == (
            "v2_directional_outcomes",
            "v2_directional_followups",
        )
        assert conn.execute(
            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN ('v2_directional_outcomes_immutable','v2_directional_followups_immutable')"
        ).fetchone() == (2,)


def test_settlement_derives_immutable_t0_outcome(database, outcome):
    journal, episode, result = outcome
    assert result == {
        "episode_id": episode,
        "closed_at_ms": 10,
        "return_pct": "-8.0848",
        "quality_score": "65",
        "closing_price": "92",
    }
    assert journal.record(episode) == result
    with database() as conn:
        row = conn.execute(
            """SELECT producer,symbol,event_type,net_pnl,evidence
            FROM v2_directional_outcomes WHERE episode_id=%s""",
            (episode,),
        ).fetchone()
    assert row[:4] == ("s6", "BTCUSDT", "TREND_UP", Decimal("-10.106"))
    assert row[4]["opening_notional"] == "125"


def test_t60_followup_is_direction_aware_idempotent_and_rolls_up(outcome):
    journal, episode, result = outcome
    observed = result["closed_at_ms"] + 3600000
    first = journal.record_t60(
        episode,
        observed_at_ms=observed,
        price="90",
        evidence={"source": "closed-public-candle", "candle_close_ms": observed},
    )
    assert first["episode_id"] == episode
    assert Decimal(first["t60_return_pct"]) == Decimal("-2.173913043478260870")
    assert (
        journal.record_t60(
            episode,
            observed_at_ms=observed,
            price="90",
            evidence={"source": "closed-public-candle", "candle_close_ms": observed},
        )
        == first
    )
    with pytest.raises(ValueError, match="T60_CONFLICT"):
        journal.record_t60(
            episode,
            observed_at_ms=observed,
            price="91",
            evidence={"source": "closed-public-candle", "candle_close_ms": observed},
        )
    history = DirectionalHistory(
        journal.connect,
        scope=SCOPE,
        producer="s6",
        clock_ms=lambda: result["closed_at_ms"] + 4000000,
    )(market_signal())
    assert history["stats"] == {
        "trades": 1,
        "win_rate": "0",
        "avg_quality_score": "65",
        "t60_avg_post_close_return_pct": "-2.17391304347826087",
        "avg_pct": "-8.0848",
    }
    assert history["evidence"]["episode_ids"] == [episode]
    assert history["evidence"]["t60_count"] == 1


def test_old_outcome_without_t60_blocks_history(outcome):
    journal, _, result = outcome
    provider = DirectionalHistory(
        journal.connect,
        scope=SCOPE,
        producer="s6",
        clock_ms=lambda: result["closed_at_ms"] + 4000000,
    )
    with pytest.raises(ValueError, match="T60_INCOMPLETE"):
        provider(market_signal())


def test_settlement_to_outcome_crash_gap_is_recovered(database, closed, monkeypatch):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    worker = service(database, income)
    original = worker.outcomes.record
    monkeypatch.setattr(
        worker.outcomes,
        "record",
        lambda _: (_ for _ in ()).throw(ConnectionError("after settlement commit")),
    )
    assert worker.run_once()["status"] == "BLOCKED"
    assert runtime.data.trace(episode)["episode"]["status"] == "SETTLED"
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_directional_outcomes").fetchone()[0]
            == 0
        )
    monkeypatch.setattr(worker.outcomes, "record", original)
    recovered = worker.run_once()
    assert recovered["status"] == "CLEAR"
    assert recovered["outcomes"][episode]["episode_id"] == episode


def test_recent_incomplete_and_other_producer_do_not_fabricate_history(outcome):
    journal, _, result = outcome
    recent = DirectionalHistory(
        journal.connect,
        scope=SCOPE,
        producer="s6",
        clock_ms=lambda: result["closed_at_ms"] + 1000,
    )(market_signal())
    assert recent["stats"]["trades"] == 1
    assert recent["stats"]["t60_avg_post_close_return_pct"] == "0"
    other = DirectionalHistory(
        journal.connect,
        scope=SCOPE,
        producer="s8",
        clock_ms=lambda: result["closed_at_ms"] + 4000000,
    )(market_signal())
    assert other["stats"] == {
        "trades": 0,
        "win_rate": "0",
        "avg_quality_score": "0",
        "t60_avg_post_close_return_pct": "0",
        "avg_pct": "0",
    }


@pytest.mark.parametrize(
    "seconds,price,evidence",
    [
        (3599, "90", {"source": "early"}),
        (3901, "90", {"source": "late"}),
        (3600, "0", {"source": "bad"}),
        (3600, "90", {}),
    ],
)
def test_invalid_t60_observation_never_persists(outcome, seconds, price, evidence):
    journal, episode, result = outcome
    with pytest.raises((ValueError, TypeError)):
        journal.record_t60(
            episode,
            observed_at_ms=result["closed_at_ms"] + seconds * 1000,
            price=price,
            evidence=evidence,
        )
    with journal.connect() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_directional_followups").fetchone()[0]
            == 0
        )
