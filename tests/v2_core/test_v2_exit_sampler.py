from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from services import v2_exit_sampler as sampler
from tests.support.v2_admission import database as database_fixture
from v2_core.evidence import canonical

database = database_fixture


@pytest.fixture
def store(database):
    with database() as conn:
        conn.execute(
            (
                Path(__file__).resolve().parents[2] / "db/research_exit_schema.sql"
            ).read_text()
        )
    return database


def config():
    return sampler.configuration("qa-frozen-study", 30000000)


def gap():
    return {
        "status": "HISTORY_GAP",
        "execution_authorized": False,
        "required_minutes": 500,
        "available_minutes": 0,
    }


class Archive:
    environment = "SANDBOX"

    def read(self, target, start, end):
        return [
            {"t": t, "o": "100", "h": "101", "l": "99", "c": "100", "v": "100"}
            for t in reversed(range(start, end, 60000))
        ]


@pytest.mark.parametrize(
    "change",
    [
        {"symbols": ["BTCUSDT"] * 4},
        {"timeframe_minutes": True},
        {"start_ms": 1},
        {"window_minutes": 10000},
        {"execution_authorized": True},
        {"retention_days": 0},
        {"parameters": {"ema_slow": 500}},
        {"study_id": "../../bad"},
    ],
)
def test_config_scope_and_bounded_work(change):
    with pytest.raises(ValueError):
        sampler.validate({**config(), **change})


def test_real_archive_pipeline_closed_nonoverlapping_cohort():
    report = sampler.sample(Archive(), config(), "BTCUSDT", 30000000)
    assert report["status"] == "SIMULATED"
    assert report["entry_start_ms"] == 30000000
    assert report["entry_end_ms"] == 51600000
    assert report["source_end_ms"] > report["entry_end_ms"]
    assert report["publication"] == "SHADOW_ONLY"
    assert report["range_volume_observations"]
    canonical(report)


def test_archive_gaps_and_wrong_scope_fail_closed():
    archive = Archive()
    archive.read = lambda *a: []
    assert (
        sampler.sample(archive, config(), "BTCUSDT", 30000000)["status"]
        == "HISTORY_GAP"
    )
    archive.environment = "LIVE"
    with pytest.raises(ValueError, match="SCOPE"):
        sampler.sample(archive, config(), "BTCUSDT", 30000000)

    class Corrupt(Archive):
        def read(self, *args):
            rows = super().read(*args)
            rows[5]["t"] += 60000
            return rows

    with pytest.raises(ValueError, match="GAP"):
        sampler.sample(Corrupt(), config(), "BTCUSDT", 30000000)


def test_register_immutable_concurrent_persist_and_no_trading_writes(store):
    c = config()
    sampler.register(store, c)
    sampler.register(store, c)
    with pytest.raises(ValueError, match="FROZEN"):
        sampler.register(store, {**c, "window_minutes": 365})

    def persist(_):
        return sampler.save(store, c, "BTCUSDT", c["start_ms"], "FORWARD", gap())

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(persist, range(4)))) == 1
    with store() as conn:
        assert conn.execute("SELECT count(*) FROM exit_samples").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT next_start_ms FROM exit_cursors WHERE symbol='BTCUSDT'"
            ).fetchone()[0]
            == 51600000
        )
        for table in ("v2_trade_intents", "v2_orders", "v2_inbound_signals"):
            assert conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    with pytest.raises(ValueError, match="IMMUTABLE"):
        sampler.save(
            store,
            c,
            "BTCUSDT",
            c["start_ms"],
            "FORWARD",
            {**gap(), "available_minutes": 1},
        )


def test_calibration_does_not_advance_forward_cursor(store):
    c = config()
    sampler.register(store, c)
    sampler.save(store, c, "BTCUSDT", 15000000, "CALIBRATION", gap())
    with store() as conn:
        assert (
            conn.execute("SELECT min(next_start_ms) FROM exit_cursors").fetchone()[0]
            == c["start_ms"]
        )
    with pytest.raises(ValueError, match="FORWARD_WINDOW"):
        sampler.save(store, c, "BTCUSDT", 15000000, "FORWARD", gap())


def test_initial_activation_cannot_relabel_past_as_forward(store):
    c = config()
    with pytest.raises(ValueError, match="MUST_BE_FUTURE"):
        sampler.register(store, c, first_activation_ms=c["start_ms"] + 1)
    sampler.register(store, c, first_activation_ms=c["start_ms"] - 1)
    sampler.register(store, c, first_activation_ms=c["start_ms"] + 100000000)


def test_horizon_wait_retry_gap_expiry_isolation_and_resume(store, monkeypatch):
    c = config()
    calls = []

    def sample(archive, c, target, start):
        calls.append((target, start))
        if target == "BTCUSDT":
            raise TimeoutError("no raw error should leak")
        return gap()

    monkeypatch.setattr(sampler, "sample", sample)
    results = sampler.run_once(store, None, c, now_ms=c["start_ms"])
    assert not calls and all(
        r["status"] == "WAITING_FOR_CLOSED_HORIZON" for r in results
    )
    ready = c["start_ms"] + (360 + 125 + 5) * 60000
    results = sampler.run_once(store, None, c, now_ms=ready)
    assert [r["status"] for r in results] == [
        "RETRY_ERROR",
        "RETRY_HISTORY_GAP",
        "RETRY_HISTORY_GAP",
    ]
    assert "no raw error" not in canonical({"r": results})
    results = sampler.run_once(store, None, c, now_ms=ready + 1440 * 60000)
    assert [r["status"] for r in results] == [
        "RETRY_ERROR",
        "HISTORY_GAP",
        "HISTORY_GAP",
    ]
    calls.clear()
    sampler.run_once(store, None, c, now_ms=ready + 1440 * 60000)
    assert calls == [
        ("BTCUSDT", c["start_ms"]),
        ("ETHUSDT", 51600000),
        ("SOLUSDT", 51600000),
    ]


def test_job_budget_defers_and_retention_only_own_study(store, monkeypatch):
    c = config()
    sampler.register(store, c)
    sampler.save(store, c, "BTCUSDT", 0, "CALIBRATION", gap())
    other = {**c, "study_id": "unrelated"}
    sampler.register(store, other)
    sampler.save(store, other, "BTCUSDT", 0, "CALIBRATION", gap())
    with store() as conn:
        conn.execute("UPDATE exit_samples SET created_at=now()-interval '100 days'")
    clock = iter((0, 181))
    result = sampler.run_once(store, None, c, now_ms=0, monotonic=lambda: next(clock))
    assert result[0]["status"] == "JOB_BUDGET_DEFERRED"
    with store() as conn:
        assert conn.execute("SELECT study_id FROM exit_samples").fetchall() == [
            ("unrelated",)
        ]


def test_db_errors_do_not_advance_cursor(store):
    c = config()
    sampler.register(store, c)
    with pytest.raises(ValueError, match="RECORD_BOUND"):
        sampler.save(
            store,
            c,
            "BTCUSDT",
            c["start_ms"],
            "FORWARD",
            {**gap(), "big": "a" * 2000000},
        )
    with store() as conn:
        assert (
            conn.execute("SELECT min(next_start_ms) FROM exit_cursors").fetchone()[0]
            == c["start_ms"]
        )
        assert conn.execute("SELECT count(*) FROM exit_samples").fetchone()[0] == 0
