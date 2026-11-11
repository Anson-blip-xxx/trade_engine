"""Pure checks for scoped observational paths; no clients, network or writes."""

import hashlib
import json
from copy import deepcopy
from decimal import Decimal

import pytest

import services.v2_setup_shadow_paths as paths
from v2_core.quant_indicators import MINUTE

START = 1792857600000
END = START + 1440 * MINUTE


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def seal(study):
    study.pop("content_digest", None)
    study["content_digest"] = digest(study)
    return study


def sample(index=1, *, side="BUY", decided_ms=None, symbol="BTCUSDT"):
    decided_ms = START + 60 * MINUTE + 1000 if decided_ms is None else decided_ms
    return {
        "opportunity_ref": f"{index:064x}",
        "symbol": symbol,
        "side": side,
        "environment": "SANDBOX",
        "observed_ms": decided_ms - 1000,
        "decided_ms": decided_ms,
        "bar_close_ms": (decided_ms - 1000) // MINUTE * MINUTE,
        "status": "BASELINE_MATCHED",
        "baseline_market_reason": "TREND_VOLUME_UNCONFIRMED",
        "alternatives": {
            "BREAKOUT_VOLUME075": {"market_reason": "CANDIDATE"},
            "PULLBACK_VOLUME075": {"market_reason": "TREND_NO_FRESH_SETUP"},
        },
    }


def study(*samples, as_of_ms=END):
    return seal(
        {
            "version": "setup-shadow-study-v1",
            "scope_key": "a" * 64,
            "account_environment": "SANDBOX",
            "window_start_ms": START,
            "window_end_ms": END,
            "as_of_ms": as_of_ms,
            "plan_digest": "b" * 64,
            "input_digest": "c" * 64,
            "samples": list(samples or [sample()]),
            "execution_authorized": False,
            "automatic_parameter_updates": False,
            "promotion_authorized": False,
        }
    )


def read(symbol, start, end):
    bars = []
    for t in range(start, end, MINUTE):
        # Anchor-minute extrema precede its close and must not affect metrics.
        bars.append(
            {
                "t": t,
                "o": "100",
                "h": "1000" if t == start else "120",
                "l": "1" if t == start else "90",
                "c": "100" if t == start else "110",
                "v": "1",
                "tbv": "0.5",
            }
        )
    return list(reversed(bars))


@pytest.mark.parametrize(
    "side,expected", [("BUY", ("10", "20", "-10")), ("SELL", ("-10", "10", "-20"))]
)
def test_long_short_closed_paths_have_directional_metrics_not_fills(side, expected):
    source = study(sample(side=side))
    before = deepcopy(source)
    calls = []

    def reader(*args):
        calls.append(args)
        return read(*args)

    result = paths.enrich_setup_paths(source, reader, as_of_ms=END)
    assert result["status"] == "AVAILABLE"
    assert result["coverage"] == {
        "query_complete": True,
        "retryable": False,
        "all_windows_observed": True,
    }
    case = result["cases"][0]
    first = START + 61 * MINUTE
    assert calls == [("BTCUSDT", first, first + 241 * MINUTE)]
    assert case["anchor_ms"] == first + MINUTE
    assert case["anchor_delay_ms"] == 119000
    assert case["anchor_price"] == "100"
    assert case["anchor_is_execution_price"] is False
    assert case["candidate_plans"] == ["BREAKOUT_VOLUME075"]
    for horizon in (30, 60, 240):
        window = case["windows"][str(horizon)]
        assert window["status"] == "OBSERVED"
        assert window["observed_end_ms"] - window["anchor_ms"] == horizon * MINUTE
        assert tuple(
            Decimal(window[key])
            for key in (
                "return_pct",
                "favorable_excursion_pct",
                "adverse_excursion_pct",
            )
        ) == tuple(map(Decimal, expected))
        assert window["costs_included"] is False
        assert window["counterfactual_execution_proven"] is False
        assert len(window["path_input_digest"]) == 64
    assert result["study_content_digest"] == source["content_digest"]
    assert result["input_digest"] == source["input_digest"]
    assert all(result[key] is False for key in paths._FLAGS)
    assert source == before
    assert len(json.dumps(result).encode()) <= 512 * 1024
    fingerprint = result.pop("content_digest")
    assert fingerprint == digest(result)


def test_exact_minute_decision_does_not_skip_the_first_full_minute():
    s = sample(decided_ms=START + 60 * MINUTE)
    result = paths.enrich_setup_paths(study(s), read, as_of_ms=END)
    assert result["cases"][0]["anchor_ms"] == START + 61 * MINUTE


def test_immature_windows_remain_pending_without_any_archive_read():
    def forbidden(*args):
        pytest.fail("immature observations must not read")

    result = paths.enrich_setup_paths(
        study(sample(decided_ms=END - MINUTE)), forbidden, as_of_ms=END
    )
    assert result["cases"][0]["status"] == "PENDING"
    assert all(w["status"] == "PENDING" for w in result["cases"][0]["windows"].values())
    assert result["budget"]["calls"] == 0
    assert result["coverage"] == {
        "query_complete": True,
        "retryable": True,
        "all_windows_observed": False,
    }


def test_partial_maturity_reads_only_largest_mature_window():
    calls = []

    def reader(*args):
        calls.append(args)
        return read(*args)

    result = paths.enrich_setup_paths(
        study(sample(decided_ms=END - 45 * MINUTE)), reader, as_of_ms=END
    )
    windows = result["cases"][0]["windows"]
    assert windows["30"]["status"] == "OBSERVED"
    assert windows["60"]["status"] == windows["240"]["status"] == "PENDING"
    assert calls[0][2] - calls[0][1] == 31 * MINUTE


def test_gap_only_suppresses_windows_requiring_that_minute():
    def reader(symbol, start, end):
        return [
            row for row in read(symbol, start, end) if row["t"] != start + 40 * MINUTE
        ]

    result = paths.enrich_setup_paths(study(), reader, as_of_ms=END)
    windows = result["cases"][0]["windows"]
    assert result["coverage"] == {
        "query_complete": True,
        "retryable": False,
        "all_windows_observed": False,
    }
    assert windows["30"]["status"] == "OBSERVED"
    for key in ("60", "240"):
        assert windows[key]["status"] == "UNKNOWN"
        assert windows[key]["reason"] == "PATH_CONTIGUOUS_MINUTES_UNAVAILABLE"
        assert windows[key]["return_pct"] is None


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda rows: rows[:-1], "PATH_CONTIGUOUS_MINUTES_UNAVAILABLE"),
        (lambda rows: [], "PATH_CONTIGUOUS_MINUTES_UNAVAILABLE"),
        (lambda rows: rows + [rows[-1]], "PATH_ARCHIVE_PAGE_BOUND"),
        (lambda rows: rows[:-1] + [rows[0]], "PATH_ARCHIVE_DUPLICATE_MINUTE"),
        (
            lambda rows: [{**r, "source": "BINANCE_PUBLIC_LIVE"} for r in rows],
            "PATH_ARCHIVE_PROVENANCE_MISMATCH",
        ),
        (
            lambda rows: [{**r, "environment": "LIVE"} for r in rows],
            "PATH_ARCHIVE_PROVENANCE_MISMATCH",
        ),
        (
            lambda rows: [{**r, "symbol": "ETHUSDT"} for r in rows],
            "PATH_ARCHIVE_PROVENANCE_MISMATCH",
        ),
        (
            lambda rows: [{**r, "t": r["t"] + 1} for r in rows],
            "PATH_ARCHIVE_TIME_MISMATCH",
        ),
        (
            lambda rows: [{**r, "t": r["t"] + 500 * MINUTE} for r in rows],
            "PATH_ARCHIVE_TIME_MISMATCH",
        ),
        (lambda rows: [{**r, "c": "NaN"} for r in rows], "PATH_ARCHIVE_BAR_INVALID"),
        (lambda rows: {"bars": rows}, "PATH_ARCHIVE_PAGE_BOUND"),
    ],
)
def test_archive_corruption_is_unknown_without_partial_metrics(change, reason):
    result = paths.enrich_setup_paths(
        study(), lambda *args: change(read(*args)), as_of_ms=END
    )
    case = result["cases"][0]
    assert case["status"] == "UNKNOWN"
    assert all(window["reason"] == reason for window in case["windows"].values())
    assert all(window["return_pct"] is None for window in case["windows"].values())


def test_optional_correct_row_provenance_is_accepted():
    def reader(*args):
        return [
            {**row, "source": paths.SOURCE, "environment": "SANDBOX", "symbol": args[0]}
            for row in read(*args)
        ]

    assert (
        paths.enrich_setup_paths(study(), reader, as_of_ms=END)["cases"][0]["status"]
        == "OBSERVED"
    )


def test_reader_exception_is_sanitized_and_remaining_cases_continue():
    def reader(symbol, start, end):
        if symbol == "BTCUSDT":
            raise RuntimeError("postgres://private_user:password@secret-host")
        return read(symbol, start, end)

    result = paths.enrich_setup_paths(
        study(sample(), sample(2, symbol="ETHUSDT")), reader, as_of_ms=END
    )
    assert (
        result["cases"][0]["windows"]["30"]["reason"] == "PATH_ARCHIVE_READ_UNAVAILABLE"
    )
    assert result["cases"][1]["status"] == "OBSERVED"
    assert "private_user" not in json.dumps(result)
    assert "secret-host" not in json.dumps(result)
    assert result["coverage"] == {
        "query_complete": False,
        "retryable": True,
        "all_windows_observed": False,
    }


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("symbol", "BTCUSDT';DROP TABLE candles", "PATH_SAMPLE_SYMBOL_OR_SIDE_INVALID"),
        ("symbol", "btcusdt", "PATH_SAMPLE_SYMBOL_OR_SIDE_INVALID"),
        ("environment", "LIVE", "PATH_SAMPLE_ENVIRONMENT_MISMATCH"),
        ("status", "UNREPLAYABLE", "PATH_BASELINE_UNPROVEN"),
        ("side", "LONG", "PATH_SAMPLE_SYMBOL_OR_SIDE_INVALID"),
        ("decided_ms", END + MINUTE, "PATH_SAMPLE_TIMING_INVALID"),
        ("bar_close_ms", END, "PATH_SAMPLE_TIMING_INVALID"),
    ],
)
def test_invalid_samples_never_reach_reader(key, value, reason):
    s = sample()
    s[key] = value
    result = paths.enrich_setup_paths(
        study(s), lambda *args: pytest.fail("invalid sample read"), as_of_ms=END
    )
    assert result["cases"][0]["status"] == "UNKNOWN"
    assert result["cases"][0]["reason"] == reason
    assert result["validation_reasons"] == {reason: 1}
    assert result["budget"]["calls"] == 0


def test_decision_day_accepts_prior_day_observation():
    s = sample(decided_ms=START)
    assert s["observed_ms"] < START
    result = paths.enrich_setup_paths(study(s), read, as_of_ms=END)
    assert result["cases"][0]["status"] == "OBSERVED"


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("account_environment", "LIVE", "PATH_SANDBOX_SCOPE_REQUIRED"),
        ("scope_key", "invalid", "PATH_SANDBOX_SCOPE_REQUIRED"),
        ("window_end_ms", END + MINUTE, "PATH_DAY_INVALID"),
        ("as_of_ms", END + MINUTE, "PATH_STUDY_BINDING_INVALID"),
        ("plan_digest", "invalid", "PATH_STUDY_BINDING_INVALID"),
        ("input_digest", "invalid", "PATH_STUDY_BINDING_INVALID"),
        ("execution_authorized", True, "PATH_RESEARCH_PERMISSIONS_INVALID"),
        ("automatic_parameter_updates", True, "PATH_RESEARCH_PERMISSIONS_INVALID"),
        ("promotion_authorized", True, "PATH_RESEARCH_PERMISSIONS_INVALID"),
    ],
)
def test_invalid_envelope_does_not_read(key, value, reason):
    source = study()
    source[key] = value
    seal(source)
    result = paths.enrich_setup_paths(
        source, lambda *args: pytest.fail("invalid study read"), as_of_ms=END
    )
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == reason
    assert result["budget"]["calls"] == 0
    assert all(result[key] is False for key in paths._FLAGS)


def test_tampering_mutable_sample_after_sealing_is_detected():
    source = study()
    source["samples"][0]["side"] = "SELL"
    result = paths.enrich_setup_paths(
        source, lambda *args: pytest.fail("tampered study read"), as_of_ms=END
    )
    assert result["reason"] == "PATH_STUDY_DIGEST_MISMATCH"


def test_reader_cannot_mutate_snapshotted_study():
    source = study()
    fingerprint = source["content_digest"]

    def reader(*args):
        source["samples"][0]["side"] = "SELL"
        source["scope_key"] = "d" * 64
        return read(*args)

    result = paths.enrich_setup_paths(source, reader, as_of_ms=END)
    assert result["cases"][0]["side"] == "BUY"
    assert result["scope_key"] == "a" * 64
    assert result["study_content_digest"] == fingerprint


def test_unicode_symbol_retains_exact_identity_and_ascii_false_digest():
    source = study(sample(symbol="币安人生USDT"))
    result = paths.enrich_setup_paths(source, read, as_of_ms=END)
    assert result["cases"][0]["symbol"] == "币安人生USDT"
    fingerprint = result.pop("content_digest")
    assert fingerprint == digest(result)
    assert (
        fingerprint
        != hashlib.sha256(
            json.dumps(
                result, sort_keys=True, ensure_ascii=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )


def test_selection_uses_fixed_time_and_ref_before_reading_and_caps_calls():
    samples = [
        sample(i, decided_ms=START + (100 - i) * MINUTE, symbol=f"ASSET{i}USDT")
        for i in range(1, 16)
    ]
    calls = []

    def reader(*args):
        calls.append(args)
        return read(*args)

    result = paths.enrich_setup_paths(study(*reversed(samples)), reader, as_of_ms=END)
    ordered = sorted(samples, key=lambda s: (s["decided_ms"], s["opportunity_ref"]))
    indices = [((2 * i + 1) * len(samples)) // 24 for i in range(12)]
    expected = [ordered[i] for i in indices]
    assert [case["opportunity_ref"] for case in result["cases"]] == [
        s["opportunity_ref"] for s in expected
    ]
    assert result["input_sample_count"] == 15
    assert [case["selection_index"] for case in result["cases"]] == indices
    assert (
        result["selection_basis"] == "FIXED_EVENLY_SPACED_DECISION_REF_METADATA_MAX_12"
    )
    assert result["selection_truncated"] is True
    assert len(calls) == result["budget"]["calls"] == 12


def test_ref_tie_breaker_and_identical_ranges_are_cached():
    first, second = sample(1), sample(2, side="SELL")
    result = paths.enrich_setup_paths(study(second, first), read, as_of_ms=END)
    assert result["selected_count"] == 2
    assert result["budget"]["calls"] == 1
    assert [row["opportunity_ref"] for row in result["cases"]] == [
        first["opportunity_ref"],
        second["opportunity_ref"],
    ]
    assert Decimal(result["cases"][1]["windows"]["30"]["return_pct"]) == -10


def test_repeated_opportunities_remain_unknown_without_backfill():
    first = sample()
    conflicting = {**first, "side": "SELL"}
    for records in (
        (first, conflicting),
        (conflicting, first),
        (first, deepcopy(first)),
    ):
        result = paths.enrich_setup_paths(
            study(*records),
            lambda *args: pytest.fail("conflicting identity read"),
            as_of_ms=END,
        )
        assert len(result["cases"]) == 2
        assert all(row["status"] == "UNKNOWN" for row in result["cases"])
        assert result["validation_reasons"] == {"PATH_DUPLICATE_OPPORTUNITY": 2}


def test_candidate_and_control_labels_do_not_affect_fixed_selection():
    a, b = sample(1), sample(2)
    a["baseline_market_reason"] = "CANDIDATE"
    a["alternatives"]["PULLBACK_VOLUME075"]["market_reason"] = "CANDIDATE"
    b["alternatives"]["BREAKOUT_VOLUME075"]["market_reason"] = (
        "TREND_VOLUME_UNCONFIRMED"
    )
    result = paths.enrich_setup_paths(study(a, b), read, as_of_ms=END)
    assert result["selected_count"] == 2
    assert result["cases"][0]["candidate_plans"] == [
        "BASELINE",
        "BREAKOUT_VOLUME075",
        "PULLBACK_VOLUME075",
    ]
    assert result["cases"][1]["candidate_plans"] == []
    assert result["cases"][1]["candidate_classification"] == "NO_PLAN_CANDIDATE_CONTROL"
    assert result["cases"][1]["status"] == "OBSERVED"


def test_equal_spacing_happens_before_validation_and_never_backfills_unknowns():
    samples = [sample(i, decided_ms=START + i * MINUTE) for i in range(1, 97)]
    indices = [((2 * i + 1) * len(samples)) // 24 for i in range(12)]
    for index in indices:
        samples[index]["status"] = "UNREPLAYABLE"
        samples[index]["bar_close_ms"] = None
    result = paths.enrich_setup_paths(
        study(*reversed(samples)),
        lambda *args: pytest.fail("unselected valid sample must not backfill"),
        as_of_ms=END,
    )
    assert result["input_sample_count"] == 96
    assert result["selected_count"] == 12
    assert result["selected_valid_count"] == 0
    assert result["status_counts"] == {"UNKNOWN": 12}
    assert [row["selection_index"] for row in result["cases"]] == indices
    assert [row["opportunity_ref"] for row in result["cases"]] == [
        samples[i]["opportunity_ref"] for i in indices
    ]
    assert result["budget"]["calls"] == 0


def test_malformed_selected_metadata_is_unknown_and_does_not_abort_valid_case():
    invalid = sample(1)
    invalid["side"] = ["BUY"]
    result = paths.enrich_setup_paths(study(invalid, sample(2)), read, as_of_ms=END)
    assert result["cases"][0]["status"] == "UNKNOWN"
    assert result["cases"][1]["status"] == "OBSERVED"


def test_input_sample_count_bound_is_enforced_before_reading():
    result = paths.enrich_setup_paths(
        study(*[sample(i) for i in range(97)]),
        lambda *args: pytest.fail("oversized input"),
        as_of_ms=END,
    )
    assert result["reason"] == "PATH_SAMPLE_BOUND"


def test_budget_leaves_three_seconds_for_each_client_bounded_read(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(paths.time, "monotonic", lambda: now[0])

    def reader(*args):
        now[0] += 3
        return read(*args)

    result = paths.enrich_setup_paths(
        study(*[sample(i, symbol=f"ASSET{i}USDT") for i in range(1, 13)]),
        reader,
        as_of_ms=END,
    )
    assert now[0] == 15
    assert result["budget"]["calls"] == 5
    assert result["status_counts"] == {"OBSERVED": 5, "UNKNOWN": 7}
    assert result["coverage"]["query_complete"] is False
    assert result["coverage"]["retryable"] is True
    assert (
        result["cases"][-1]["windows"]["30"]["reason"]
        == "PATH_ARCHIVE_BUDGET_EXHAUSTED"
    )


def test_a_reader_overrun_does_not_emit_late_metrics_or_start_more_reads(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(paths.time, "monotonic", lambda: now[0])

    def reader(*args):
        now[0] += 16
        return read(*args)

    result = paths.enrich_setup_paths(
        study(sample(), sample(2, symbol="ETHUSDT")), reader, as_of_ms=END
    )
    assert result["budget"]["calls"] == 1
    assert result["status_counts"] == {"UNKNOWN": 2}
    assert result["cases"][0]["windows"]["30"]["return_pct"] is None


def test_bad_reader_and_nonserializable_inputs_are_isolated():
    result = paths.enrich_setup_paths(study(), None, as_of_ms=END)
    assert result["reason"] == "PATH_READER_INVALID"
    assert result["coverage"]["retryable"] is False
    assert result["scope_key"] == "a" * 64
    assert result["input_digest"] == "c" * 64
    source = study()
    source["private_value"] = object()
    result = paths.enrich_setup_paths(source, read, as_of_ms=END)
    assert result["reason"] == "PATH_STUDY_VALIDATION_UNAVAILABLE"
    assert result["coverage"]["retryable"] is False
    assert result["budget"]["calls"] == 0


def test_live_study_is_bound_unknown_and_never_reads_sandbox_market():
    source = study()
    source["account_environment"] = "LIVE"
    seal(source)
    result = paths.enrich_setup_paths(
        source,
        lambda *args: pytest.fail("LIVE study cannot read SANDBOX market"),
        as_of_ms=END,
    )
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "PATH_SANDBOX_SCOPE_REQUIRED"
    assert result["coverage"]["retryable"] is False
    assert result["scope_key"] == source["scope_key"]
    assert result["account_environment"] == result["environment"] == "LIVE"
    assert result["window_start_ms"] == START
    assert result["window_end_ms"] == END
    assert result["study_content_digest"] == source["content_digest"]
    assert result["budget"]["calls"] == 0


def test_scope_plan_and_input_binding_change_all_observation_digests():
    baseline = paths.enrich_setup_paths(study(), read, as_of_ms=END)
    original = baseline["cases"][0]["windows"]["30"]["path_input_digest"]
    for key in ("scope_key", "plan_digest", "input_digest"):
        source = study()
        source[key] = "e" * 64
        result = paths.enrich_setup_paths(seal(source), read, as_of_ms=END)
        assert result["cases"][0]["windows"]["30"]["path_input_digest"] != original
