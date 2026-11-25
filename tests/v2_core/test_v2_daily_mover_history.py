"""History memory bounds and unchanged statistics, including disposable PG."""

import json
from collections import Counter
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

import pytest

from services import v2_daily_mover_history as history
from services import v2_daily_mover_store as store_module
from services.v2_daily_mover_report import (
    DAY,
    MINIMUM_CASES,
    MINIMUM_DAYS,
    VERSION,
    _accumulate,
    _summary,
    format_digest,
    seal_report,
)
from tests.support.v2_admission import database as database_fixture
from tests.v2_core.test_v2_daily_mover_report import SCOPE, build

database = database_fixture


@pytest.fixture
def store(database):
    ddl = (
        Path(__file__).resolve().parents[2] / "db/migrations/20261026_daily_movers.sql"
    ).read_text()
    with database() as connection:
        connection.execute(ddl)
    return store_module.DailyMoverStore(database)


def previous(report=None, days=1):
    report = deepcopy(report or build())
    report["window"]["start_ms"] -= days * DAY
    report["window"]["end_ms"] -= days * DAY
    report["as_of_ms"] -= days * DAY
    return report


def minimal(report):
    """Independent Python projection oracle; preserve presence and JSON null."""
    result = {
        key: deepcopy(report[key])
        for key in (
            "version",
            "as_of_ms",
            "market_source",
            "scope_key",
            "market_digest",
        )
        if key in report
    }
    result["window"] = {"start_ms": report["window"]["start_ms"]}
    result["market_comparison"] = {
        cohort: [
            {key: value for key, value in row.items() if key != "symbol"}
            for row in report["market_comparison"][cohort]
        ]
        for cohort in ("selected_samples", "control_samples")
    }
    result["rows"] = [
        {"evidence": {"primary_reason": row["evidence"]["primary_reason"]}}
        for row in report["rows"]
    ]
    return result


def legacy_accumulate(current, prior_reports):
    """Frozen pre-projection accumulation algorithm (2026-11-25)."""
    earliest = current["window"]["start_ms"] - 29 * DAY
    market_days, account_days, ignored = {}, {}, Counter()
    for report in (*prior_reports, current):
        if not isinstance(report, dict) or report.get("version") != VERSION:
            ignored["INCOMPATIBLE_VERSION"] += 1
            continue
        start, known = report.get("window", {}).get("start_ms"), report.get("as_of_ms")
        if (
            type(start) is not int
            or not earliest <= start <= current["window"]["start_ms"]
            or type(known) is not int
            or known > current["as_of_ms"]
        ):
            ignored["OUTSIDE_OBSERVABLE_30_DAY_WINDOW"] += 1
            continue
        key = (report.get("market_source"), start)
        if report.get("market_source") != current["market_source"]:
            ignored["DIFFERENT_MARKET_SOURCE"] += 1
            continue
        old = market_days.get(key)
        if old is None or (known, report.get("market_digest", "")) > (
            old["as_of_ms"],
            old.get("market_digest", ""),
        ):
            market_days[key] = report
        if report.get("scope_key") == current["scope_key"]:
            old = account_days.get(start)
            if old is None or known >= old["as_of_ms"]:
                account_days[start] = report
    selected, controls, reasons = [], [], Counter()
    for report in market_days.values():
        sample = report.get("market_comparison", {})
        selected.extend(sample.get("selected_samples", []))
        controls.extend(sample.get("control_samples", []))
    for report in account_days.values():
        for row in report.get("rows", []):
            reasons[
                row.get("evidence", {}).get("primary_reason", "EVIDENCE_UNAVAILABLE")
            ] += 1
    ready = (
        len(market_days) >= MINIMUM_DAYS
        and len(selected) >= MINIMUM_CASES
        and len(controls) >= MINIMUM_CASES
    )
    return {
        "window_days": 30,
        "unique_market_days": len(market_days),
        "unique_account_days": len(account_days),
        "selected": _summary(selected),
        "controls": _summary(controls),
        "account_symbol_day_reason_counts": dict(sorted(reasons.items())),
        "ignored_reports": dict(ignored),
        "sample_status": "DESCRIPTIVE_COMPARISON_ONLY"
        if ready
        else "INSUFFICIENT_SAMPLE",
        "minimum_days": MINIMUM_DAYS,
        "minimum_cases_per_cohort": MINIMUM_CASES,
        "causal_edge_proven": False,
        "automatic_parameter_updates": False,
        "limitations": [
            "OUTCOME_SELECTED_COHORT_NOT_RANDOMIZED",
            "WHOLE_DAY_FEATURES_NOT_AVAILABLE_AT_ENTRY",
            "CONTROL_COHORT_NOT_LIQUIDITY_OR_REGIME_MATCHED",
            "SAMPLE_THRESHOLD_NOT_STATISTICAL_SIGNIFICANCE",
        ],
    }


def test_full_and_minimal_history_equal_frozen_statistics():
    current = build()
    reports = [previous(current, i) for i in range(1, 31)]
    reports[0]["market_comparison"]["selected_samples"][0]["return_pct"] = None
    reports[1]["market_comparison"]["control_samples"] = []
    reports[2]["market_comparison"]["control_samples"][0].pop("return_pct")
    other = deepcopy(reports[3])
    other.update(scope_key="other", market_digest="f" * 64)
    foreign = deepcopy(reports[4])
    foreign["market_source"] = "OTHER_MARKET"
    future = deepcopy(reports[5])
    future["as_of_ms"] = current["as_of_ms"] + 1
    reports += [other, foreign, future, deepcopy(reports[0])]
    expected = legacy_accumulate(current, reports)
    for supplied in (reports, [minimal(row) for row in reports]):
        actual = _accumulate(current, supplied)
        coverage = actual.pop("history_coverage")
        assert coverage["status"] == "READ_COMPLETE"
        assert actual == expected


def test_decoder_preserves_exact_payload_unicode_and_null():
    payload = {"scope_key": "账户", "rows": None}
    encoded = json.dumps(payload, ensure_ascii=False)
    rows = [(date(2026, 10, 24), len(encoded.encode()), encoded, None)]
    assert history.decode_history(rows, "2026-10-25") == [payload]


@pytest.mark.parametrize("encoded", [None, "not json", "[]", "null", '"text"'])
def test_bad_wire_payload_is_explicit_unknown(encoded):
    size = len(encoded.encode()) if isinstance(encoded, str) else 0
    decoded = history.decode_history(
        [(date(2026, 10, 24), size, encoded, None)], "2026-10-25"
    )
    assert decoded[0][history.ERROR_KEY] == "HISTORY_PROJECTION_INVALID"


@pytest.mark.parametrize("bad", [[], None, 1, "UNKNOWN_NEW_REASON"])
def test_bad_error_marker_cannot_crash_current_report(bad):
    actual = _accumulate(build(), [{history.ERROR_KEY: bad}])
    assert actual["history_coverage"]["status"] == "PARTIAL_UNKNOWN"
    assert actual["unique_market_days"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"window": None},
        {"window": {}},
        {"window": {"start_ms": None}},
        {"rows": None},
        {"market_comparison": None},
        {"rows": [None]},
        {"rows": [{"evidence": None}]},
        {"rows": [{"evidence": {"primary_reason": []}}]},
        {"market_comparison": {"selected_samples": [], "control_samples": [None]}},
        {
            "market_comparison": {
                "selected_samples": [],
                "control_samples": [{"return_pct": "NaN"}],
            }
        },
    ],
)
def test_corrupt_latest_day_does_not_abort_or_appear_as_zero(change):
    archived = previous()
    archived.update(change)
    current = build(prior_reports=[archived])
    accumulation = current["accumulation"]
    assert accumulation["history_coverage"]["unusable_revisions"] == 1
    assert accumulation["unique_market_days"] == 1
    assert accumulation["selected"]["count"] == 2
    assert "累计历史不完整" in format_digest(current)


def test_decode_bounds_reject_oversize_before_json_parse(monkeypatch):
    monkeypatch.setattr(history.json, "loads", lambda _: pytest.fail("must not decode"))
    decoded = history.decode_history(
        [(date(2026, 10, 24), history.MAX_DAY_BYTES + 1, "invalid", None)],
        "2026-10-25",
    )
    assert decoded[0][history.ERROR_KEY] == "HISTORY_DAY_TOO_LARGE"


def test_decoder_cannot_assign_latest_report_to_different_natural_day():
    encoded = json.dumps(minimal(previous()))
    decoded = history.decode_history(
        [(date(2026, 10, 23), len(encoded.encode()), encoded, None)],
        "2026-10-25",
    )
    assert decoded[0][history.ERROR_KEY] == "HISTORY_PROJECTION_INVALID"


def test_decode_total_bound_and_duplicates(monkeypatch):
    monkeypatch.setattr(history, "MAX_HISTORY_BYTES", 3)
    decoded = history.decode_history(
        [(date(2026, 10, 24), 2, "{}", None), (date(2026, 10, 23), 2, "{}", None)],
        "2026-10-25",
    )
    assert decoded == [
        {},
        history.history_error(date(2026, 10, 23), "HISTORY_TOTAL_TOO_LARGE"),
    ]
    with pytest.raises(ValueError, match="ROW_INVALID"):
        history.decode_history([(date(2026, 10, 24), 2, "{}", None)] * 2, "2026-10-25")


def test_sql_projection_preserves_statistics_and_omits_large_unused_evidence(store):
    full = previous()
    full["account_evidence"] = {"unused": "x" * (2 * history.MAX_DAY_BYTES)}
    full["rows"][0]["evidence"]["trades"] = [{"unused": "y" * 10000}]
    full["market_comparison"]["selected_samples"][0]["return_pct"] = None
    store.save_report(SCOPE, "2026-10-24", full)
    projected = store.prior(SCOPE, "2026-10-25")
    assert projected == [minimal(full)]
    assert len(json.dumps(projected)) < 4096
    assert _accumulate(build(), projected) == _accumulate(build(), [full])
    assert store.existing(SCOPE, "2026-10-24")[1] == full


def test_latest_bad_revision_retained_not_older_good_revision(store):
    full = previous()
    store.save_report(SCOPE, "2026-10-24", full)
    bad = {**full, "market_comparison": None}
    store.save_report(SCOPE, "2026-10-24", bad)
    projected = store.prior(SCOPE, "2026-10-25")
    assert projected[0]["market_comparison"] is None
    result = _accumulate(build(), projected)
    assert result["history_coverage"]["status"] == "PARTIAL_UNKNOWN"
    assert result["unique_market_days"] == 1


def test_sql_does_not_normalize_missing_fields_into_null(store):
    full = previous()
    full["market_comparison"]["selected_samples"][0].pop("return_pct")
    full["market_comparison"]["selected_samples"][1]["return_pct"] = None
    store.save_report(SCOPE, "2026-10-24", full)
    projected = store.prior(SCOPE, "2026-10-25")[0]
    samples = projected["market_comparison"]["selected_samples"]
    assert "return_pct" not in samples[0]
    assert samples[1]["return_pct"] is None
    assert _accumulate(build(), [projected]) == _accumulate(build(), [full])


def test_sql_day_limit_does_not_transfer_or_decode_oversize(store):
    full = previous()
    store.save_report(SCOPE, "2026-10-24", full)
    bad = {**full, "market_digest": "x" * (history.MAX_DAY_BYTES + 1)}
    store.save_report(SCOPE, "2026-10-24", bad)
    projected = store.prior(SCOPE, "2026-10-25")
    assert projected == [
        history.history_error(date(2026, 10, 24), "HISTORY_DAY_TOO_LARGE")
    ]


def test_sql_total_limit_keeps_explicit_unknown_day(store, monkeypatch):
    monkeypatch.setattr(history, "MAX_HISTORY_BYTES", 1500)
    monkeypatch.setattr(store_module, "MAX_HISTORY_BYTES", 1500)
    for i in range(1, 4):
        day = date(2026, 10, 25) - timedelta(days=i)
        store.save_report(SCOPE, day.isoformat(), previous(days=i))
    projected = store.prior(SCOPE, "2026-10-25")
    assert len(projected) == 3
    assert projected[0].get(history.ERROR_KEY) is None
    assert projected[-1][history.ERROR_KEY] == "HISTORY_TOTAL_TOO_LARGE"
    result = _accumulate(build(), projected)
    assert result["history_coverage"]["status"] == "PARTIAL_UNKNOWN"


def test_history_coverage_is_part_of_sealed_report():
    report = build(prior_reports=[{history.ERROR_KEY: "HISTORY_DAY_TOO_LARGE"}])
    assert report == seal_report(report)
