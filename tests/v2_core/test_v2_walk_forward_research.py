"""Archive integration smoke: fake read-only ClickHouse, real aggregation/replay."""

import json
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from services.v2_walk_forward_research import QUERY_SETTINGS, main, run
from v2_core.evidence import canonical, digest

MINUTE = 60000
PARAMS = {
    "period": 2,
    "channel_period": 3,
    "band_period": 3,
    "ema_fast": 2,
    "ema_slow": 3,
    "volume_period": 3,
    "min_rvol": "1",
}
CANDIDATES = [
    {"id": "donchian-small", "plugin": "donchian", "params": PARAMS},
    {"id": "squeeze-small", "plugin": "squeeze", "params": PARAMS},
]


class Archive:
    def __init__(self):
        self.rows, self.queries = [], []
        self.closed = False

    def seed(self, *, environment="LIVE", target="BTCUSDT", count=60, scale=1):
        for index in range(count):
            price = (102 if 9 <= index % 18 <= 13 else 100) * scale
            bar = {
                "t": index * MINUTE,
                "o": str(price),
                "h": str(price + 1),
                "l": str(price - 1),
                "c": str(price),
                "v": "30" if index % 18 in (9, 14) else "10",
            }
            payload = canonical(bar)
            self.rows.append((environment, target, bar["t"], payload, digest(payload)))
        return self

    def query(self, sql, *, parameters, settings):
        assert sql.startswith("SELECT ")
        assert settings == QUERY_SETTINGS
        self.queries.append((sql, dict(parameters), dict(settings)))
        return SimpleNamespace(
            result_rows=[
                (stamp, payload, fingerprint.encode("ascii"))
                for environment, target, stamp, payload, fingerprint in self.rows
                if environment == parameters["environment"]
                and target == parameters["symbol"]
                and parameters["start"] <= stamp < parameters["end"]
            ]
        )

    def insert(self, *args, **kwargs):
        raise AssertionError("Research must not write the archive")

    def close(self):
        self.closed = True


def research(client=None, **changes):
    return run(
        Archive().seed() if client is None else client,
        **{
            "symbols": ("BTCUSDT",),
            "environment": "LIVE",
            "timeframe": 1,
            "end_ms": 60 * MINUTE,
            "minutes": 60,
            "train_bars": 24,
            "test_bars": 12,
            "candidates": CANDIDATES,
            **changes,
        },
    )


def test_read_only_archive_smoke_runs_real_replay_and_binds_reports():
    client = Archive().seed()
    before = deepcopy(client.rows)
    report = research(client)
    assert client.rows == before
    assert not client.closed  # run never owns or closes its caller's client
    assert len(client.queries) == 1
    assert report["execution_authorized"] is report["hmm_fitted"] is False
    assert report["portfolio_equity"] is None
    assert report["input_closed_at_utc8"].endswith("+08:00")
    assert report["read_budget"]["requested_minutes"] == 60
    assert report["counts"]["folds"] == 3
    source = report["reports"][0]
    assert source["status"] == "SIMULATED"
    assert source["source_environment"] == "LIVE"
    assert source["raw_result_version"] == "quant-walk-forward-v1"
    assert source["raw_result_digest"] == digest(canonical(source["walk_forward"]))
    assert report["raw_report_digest"] == digest(
        canonical(
            {key: value for key, value in report.items() if key != "raw_report_digest"}
        )
    )
    assert all(
        item["assessment"]["status"] == "INSUFFICIENT_EVIDENCE"
        for item in source["fold_acceptance"]
    )
    assert source["sample_count"] == sum(
        item["manifest"]["sample_count"] for item in source["fold_acceptance"]
    )
    for item in source["fold_acceptance"]:
        manifest = item["manifest"]
        assert manifest["code_digest"] is None
        assert manifest["capital_scope"] == "SINGLE_SYMBOL"
        assert manifest["fold_count"] == 1
        assert manifest["metrics"]["cost_stress_net_pnl"] is None
        assert manifest["historical_arrival_proven"] is False
        assert manifest["historical_universe_proven"] is False
        assert manifest["funding_complete"] is False
        assert manifest["independent_validation"] is False
        assert manifest["cost_stress_tested"] is False
    json.dumps(report, allow_nan=False)


def test_environment_isolation_changes_source_digest_without_cross_environment_reads():
    client = Archive().seed().seed(environment="SANDBOX", scale=2)
    live = research(client)
    sandbox = research(client, environment="SANDBOX")
    assert live["reports"][0]["input_digest"] != sandbox["reports"][0]["input_digest"]
    assert [query[1]["environment"] for query in client.queries] == ["LIVE", "SANDBOX"]
    assert sandbox["source_environment"] == "SANDBOX"


def test_missing_history_is_not_fetched_backfilled_or_replayed():
    client = Archive().seed(count=59)
    report = research(client)
    assert report["reports"][0]["status"] == "INCOMPLETE_HISTORY"
    assert report["reports"][0]["available_minutes"] == 59
    assert "walk_forward" not in report["reports"][0]
    assert (
        report["counts"]["folds"]
        == report["counts"]["selected_settled_oos_samples"]
        == 0
    )
    assert len(client.queries) == 1


def test_only_fully_closed_timeframe_bars_are_read_and_aggregated():
    client = Archive().seed(count=67)
    report = research(
        client,
        timeframe=5,
        end_ms=67 * MINUTE,
        minutes=60,
        train_bars=9,
        test_bars=3,
    )
    assert report["input_closed_at_ms"] == 65 * MINUTE
    assert client.queries[0][1]["start"] == 5 * MINUTE
    assert client.queries[0][1]["end"] == 65 * MINUTE
    source = report["reports"][0]
    assert source["bar_count"] == 12
    assert source["walk_forward"]["folds"][0]["test"]["end_ms_exclusive"] == 65 * MINUTE


def test_paginated_reads_are_cumulatively_bounded_across_symbols():
    client = Archive().seed(count=2100).seed(target="ETHUSDT", count=2100)
    report = research(
        client,
        symbols=("BTCUSDT", "ETHUSDT"),
        timeframe=60,
        end_ms=2100 * MINUTE,
        minutes=2100,
        train_bars=30,
        test_bars=5,
    )
    assert len(client.queries) == 6
    assert report["read_budget"]["requested_minutes"] == 4200
    assert all(
        (query[1]["end"] - query[1]["start"]) // MINUTE <= 1000
        for query in client.queries
    )
    assert all(
        query[2]["max_threads"] == 1 and query[2]["max_execution_time"] == 5
        for query in client.queries
    )
    assert report["counts"]["symbols"] == 2
    assert report["portfolio_equity"] is None


def test_defaults_are_actual_registered_presets_with_explicit_insufficient_samples():
    report = research(candidates=None, train_bars=None, test_bars=None)
    source = report["reports"][0]
    assert source["status"] == "INSUFFICIENT_DATA"
    assert report["train_bars"] <= source["bar_count"]
    assert {
        candidate["plugin"] for candidate in source["walk_forward"]["candidates"]
    } == {"donchian", "squeeze"}
    assert "NO_COMPLETE_OOS_FOLD" in source["walk_forward"]["flags"]


@pytest.mark.parametrize(
    "changes",
    [
        {"symbols": ()},
        {"symbols": ("BTCUSDT",) * 2},
        {"symbols": ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")},
        {"symbols": ([],)},
        {"environment": "TESTNET"},
        {"environment": None},
        {"timeframe": True},
        {"timeframe": 2},
        {"minutes": True},
        {"minutes": 0},
        {"minutes": 14401},
        {"symbols": ("BTCUSDT", "ETHUSDT"), "minutes": 7201},
        {"minutes": 10001, "end_ms": 10001 * MINUTE},
        {"end_ms": -1},
        {"end_ms": 60 * MINUTE + 1},
        {"end_ms": True},
        {"timeframe": 5, "end_ms": 61 * MINUTE, "minutes": 61},
        {"train_bars": -1},
        {"train_bars": False},
        {"test_bars": 0},
        {"gap_bars": -1},
        {"gap_bars": True},
        {"candidates": []},
        {"candidates": CANDIDATES * 2},
        {"candidates": [{"id": "other", "plugin": "vwap_reversion", "params": {}}]},
        {"code_digest": "unknown"},
    ],
)
def test_invalid_scope_fails_before_any_archive_reads(changes):
    client = Archive()
    with pytest.raises((ValueError, TypeError)):
        research(client, **changes)
    assert client.queries == []


def test_explicit_code_digest_is_preserved_without_claiming_acceptance():
    report = research(code_digest="a" * 64)
    assert report["code_digest"] == "a" * 64
    for item in report["reports"][0]["fold_acceptance"]:
        assert item["manifest"]["code_digest"] == "a" * 64
        assert item["assessment"]["status"] == "INSUFFICIENT_EVIDENCE"


def test_cli_runs_fake_archive_prints_json_and_closes_its_read_only_client(
    monkeypatch, capsys
):
    client = Archive().seed(count=120)
    options = []
    monkeypatch.setitem(
        sys.modules,
        "clickhouse_connect",
        SimpleNamespace(get_client=lambda **kwargs: options.append(kwargs) or client),
    )
    main(
        [
            "--symbols",
            "BTCUSDT",
            "--environment",
            "LIVE",
            "--timeframe",
            "1",
            "--end-ms",
            str(120 * MINUTE),
            "--minutes",
            "120",
            "--train-bars",
            "80",
            "--test-bars",
            "20",
            "--gap-bars",
            "0",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert report["reports"][0]["status"] == "SIMULATED"
    assert report["counts"]["folds"] == 2
    assert report["execution_authorized"] is False
    assert client.closed
    assert options[0]["settings"] == QUERY_SETTINGS
    assert options[0]["query_retries"] == 0
    assert options[0]["send_receive_timeout"] == 5
    assert "username" not in options[0] and "password" not in options[0]


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--end-ms", "-1"),
        ("--minutes", "-1"),
        ("--train-bars", "-1"),
        ("--test-bars", "-1"),
        ("--gap-bars", "-1"),
        ("--train-bars", "0"),
        ("--minutes", "0"),
    ],
)
def test_cli_nonnegative_validation_happens_before_connect(flag, value, monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("invalid CLI must not connect")

    monkeypatch.setitem(
        sys.modules, "clickhouse_connect", SimpleNamespace(get_client=forbidden)
    )
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "--symbols",
                "BTCUSDT",
                "--environment",
                "LIVE",
                "--end-ms",
                str(60 * MINUTE),
                flag,
                value,
            ]
        )
    assert caught.value.code == 2
