import json
from dataclasses import replace
from decimal import Decimal
from functools import partial

import pytest
from test_v2_quant_replay import scenario

from services.v2_range_volume_study import archive_study, compare, persist
from tests.support.v2_admission import database as database_fixture
from v2_core.evidence import canonical
from v2_core.quant_indicators import Bar
from v2_core.quant_replay import ReplayPolicy, replay
from v2_core.range_volume import RangeVolumePolicy, assess, gate

database = database_fixture


def compression(side="BUY", volume="300"):
    bars = [Bar(i * 300000, "100", "101", "99", "100", "100") for i in range(30)]
    bars.append(
        Bar(9000000, "100", "105", "99", "104" if side == "BUY" else "96", volume)
        if side == "BUY"
        else Bar(9000000, "100", "101", "95", "96", volume)
    )
    return bars


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_prior_only_levels_and_symmetric_volume(side):
    bars = compression(side)
    result = assess(bars, timeframe_minutes=5)
    assert result["side"] == side
    assert result["range_pass"] and result["volume_pass"]
    assert result["facts"]["range_high"] == "101"
    assert result["facts"]["range_low"] == "99"
    assert result["facts"]["prior_mean_tr"] == "2"
    assert result["facts"]["relative_volume"] == "3"
    assert result["closed_at_ms"] == 9300000
    canonical(result)


def test_filters_are_independent_and_unknown_volume_not_positive_evidence():
    bars = compression(volume="10")
    assert gate(bars, "BUY", timeframe_minutes=5, variant="range")["allow"]
    assert not gate(bars, "BUY", timeframe_minutes=5, variant="volume")["allow"]
    zero = [replace(b, v="0") for b in compression()]
    result = assess(zero, timeframe_minutes=5)
    assert result["facts"]["relative_volume"] is None
    assert not result["volume_pass"]
    assert not gate(bars, "SELL", timeframe_minutes=5, variant="combined")["allow"]


@pytest.mark.parametrize(
    "patch",
    [
        {"range_bars": True},
        {"range_bars": 201},
        {"atr_bars": 1},
        {"max_width_atr": "NaN"},
        {"min_relative_volume": 1.5},
        {"max_efficiency": "2"},
        {"min_close_strength": "0"},
        {"breakout_buffer_atr": "2", "max_extension_atr": "1"},
    ],
)
def test_thresholds_are_typed_bounded_and_configurable(patch):
    with pytest.raises(ValueError):
        RangeVolumePolicy(**patch)


def test_gap_duplicate_flat_warmup_and_no_mutation():
    bars = compression()
    original = list(bars)
    assert assess(bars[:3], timeframe_minutes=5)["status"] == "WARMUP"
    for corrupt in (bars[:-2] + bars[-1:], bars + bars[-1:]):
        with pytest.raises(ValueError, match="HISTORY"):
            assess(corrupt, timeframe_minutes=5)
    flat = [Bar(i * 300000, "100", "100", "100", "100", "0") for i in range(30)]
    result = assess(flat, timeframe_minutes=5)
    assert not result["range_pass"] and result["facts"]["width_atr"] is None
    assert bars == original


def test_replay_default_unchanged_and_gate_is_not_future_aware():
    bars = scenario(5)
    baseline = replay(bars, timeframe_minutes=5)
    adapted = replay(
        bars,
        timeframe_minutes=5,
        entry_gate=partial(gate, timeframe_minutes=5, variant="baseline"),
    )
    assert baseline["trades"] == adapted["trades"]
    assert baseline["summary"] == adapted["summary"]
    assert baseline["open_position"] == adapted["open_position"]
    earlier = replay(
        bars[:-2],
        timeframe_minutes=5,
        entry_gate=partial(gate, timeframe_minutes=5, variant="combined"),
    )
    bars[-1] = Bar(bars[-1].t, "102", "200", "1", "100", "1000000")
    later = replay(
        bars,
        timeframe_minutes=5,
        entry_gate=partial(gate, timeframe_minutes=5, variant="combined"),
    )
    assert earlier["decisions"] == later["decisions"][: len(earlier["decisions"])]
    assert earlier["curve"] == later["curve"][: len(earlier["curve"])]


@pytest.mark.parametrize("verdict", [{}, {"allow": 1}, {"allow": "yes"}])
def test_invalid_gate_verdict_fails_closed(verdict):
    with pytest.raises(ValueError, match="GATE_VERDICT"):
        replay(scenario(), timeframe_minutes=5, entry_gate=lambda *args: verdict)


@pytest.fixture(scope="module")
def study():
    return compare(scenario(8), timeframe_minutes=5)


def test_four_way_comparison_cost_stress_and_flat_holdout(study):
    assert not study["execution_authorized"] and study["publication"] == "SHADOW_ONLY"
    assert study["holdout_starts_at_utc8"].endswith("+08:00")
    assert set(study["folds"]) == {"development", "holdout"}
    assert set(study["double_cost_holdout"]) == {
        "baseline",
        "range",
        "volume",
        "combined",
    }
    for name, fold in study["folds"].items():
        for result in fold["variants"].values():
            assert (
                result["summary"]["candidate_count"]
                >= result["summary"]["allowed_candidates"]
            )
            if name == "holdout":
                assert all(
                    t["opened_at_ms"] >= study["holdout_starts_at_ms"]
                    for t in result["trades"]
                )
                assert all(
                    d["closed_at_ms"] >= study["holdout_starts_at_ms"]
                    for d in fold["candidate_observations"]
                )
    canonical(study)
    assert study == compare(scenario(8), timeframe_minutes=5)


def test_budget_and_incomplete_input_never_publish():
    ticks = iter([0, 121])
    with pytest.raises(TimeoutError, match="COMPUTE_BUDGET"):
        compare(scenario(), timeframe_minutes=5, monotonic=lambda: next(ticks))

    class Archive:
        environment = "SANDBOX"

        def read(self, *args):
            return []

    result = archive_study(Archive(), target="BTCUSDT", end_ms=86400000, minutes=1440)
    assert result["status"] == "INCOMPLETE_HISTORY"
    assert result["execution_authorized"] is False
    with pytest.raises(ValueError, match="PERSIST_SCOPE"):
        persist(lambda: pytest.fail("must not access DB"), result)


def test_pg_research_is_immutable_idempotent_and_has_no_trading_effect(database, study):
    report = {
        "status": "SIMULATED",
        "environment": "SANDBOX",
        "symbol": "BTCUSDT",
        **study,
    }
    before = canonical(report)
    first = persist(database, report)
    assert persist(database, report) == first
    assert canonical(report) == before
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_inbound_signals").fetchone() == (
            0,
        )
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'='range-volume-study-v1'"
        ).fetchone() == (1,)
        saved = conn.execute(
            "SELECT payload FROM v2_business_state WHERE state_id=%s",
            (first["state_id"],),
        ).fetchone()[0]
    assert saved == json.loads(before)
    changed = {
        **report,
        "range_policy": {**report["range_policy"], "min_relative_volume": "2"},
    }
    assert persist(database, changed)["study_id"] != first["study_id"]


def test_cost_summary_is_realized_not_mark_to_market(study):
    for result in study["folds"]["holdout"]["variants"].values():
        assert Decimal(result["summary"]["realized_net_pnl"]) == sum(
            (Decimal(t["net_pnl"]) for t in result["trades"]), Decimal(0)
        )
        if not result["trades"]:
            assert result["summary"]["win_rate"] is None


def test_concurrent_pg_writers_share_one_immutable_record(database, study):
    from concurrent.futures import ThreadPoolExecutor

    report = {
        "status": "SIMULATED",
        "environment": "SANDBOX",
        "symbol": "ETHUSDT",
        **study,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: persist(database, report), range(2)))
    assert results[0] == results[1]
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_state_history WHERE state_id=%s",
            (results[0]["state_id"],),
        ).fetchone() == (1,)


def test_verified_archive_full_chain():
    from test_v2_quant_indicators import minutes
    from test_v2_shared_market import CH

    from services.v2_shared_market import IncrementalArchive
    from v2_core.evidence import digest

    client = CH()
    for row in minutes(breakout=True):
        payload = canonical(row)
        client.rows.append(("SANDBOX", "BTCUSDT", row["t"], payload, digest(payload)))
    result = archive_study(
        IncrementalArchive(client, "SANDBOX"),
        target="BTCUSDT",
        end_ms=86400000,
        minutes=1440,
    )
    assert result["status"] == "SIMULATED"
    assert len(result["folds"]["holdout"]["variants"]) == 4
    assert client.inserts == []
    opened, target, stamp, payload, _fingerprint = client.rows[-1]
    client.rows[-1] = (opened, target, stamp, payload, "invalid")
    with pytest.raises(ValueError, match="ARCHIVE_CONFLICT"):
        archive_study(
            IncrementalArchive(client, "SANDBOX"),
            target="BTCUSDT",
            end_ms=86400000,
            minutes=1440,
        )


def test_replay_reports_quantity_and_chase_rejections():
    result = replay(
        scenario(1), timeframe_minutes=5, policy=ReplayPolicy(minimum_quantity="100")
    )
    assert result["summary"]["entry_rejections"] == {"QUANTITY_BELOW_MINIMUM": 1}
    assert result["entry_attempts"][0]["status"] == "NOT_FILLED"
    assert not result["trades"] and result["open_position"] is None
    bars = scenario(1)
    bars[-1] = Bar(bars[-1].t, "120", "121", "119", "120", "10")
    assert replay(bars, timeframe_minutes=5)["summary"]["entry_rejections"] == {
        "NEXT_OPEN_CHASE_LIMIT": 1
    }


@pytest.mark.parametrize(
    "row",
    [
        ("postgres", "trade_v2", "v2_business_state"),
        ("trade_v2_testnet", "public", "trade_v2.v2_business_state"),
        ("trade_v2_testnet", "trade_v2", None),
    ],
)
def test_wrong_database_is_rejected_before_study(row):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from services.v2_range_volume_study_entry import validate_sink

    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda _: SimpleNamespace(fetchone=lambda: row))

    with pytest.raises(ValueError, match="DATABASE_SCOPE"):
        validate_sink(connect)
