"""Directional attribution, representative examples and frozen numeric exports."""

import json

from services.v2_daily_mover_evidence import (
    CLASSIFICATION_VERSION,
    DAY_MS,
    OFFSET_MS,
    SIGNAL_WINDOW_MS,
    _frozen_details,
    _lifecycle,
    summarize_symbol_evidence,
)

START = 20000 * DAY_MS - OFFSET_MS


def record(index=0, **changes):
    return {
        "signal_id": str(index),
        "source": "s3",
        "event": "TREND_UP",
        "observed_ms": START + index,
        "received_ms": START + index,
        "expires_ms": START + index + 300000,
        "consumer_strategy": "s6",
        "decision_id": f"decision-{index}",
        "decided_ms": START + index + 1000,
        "action": "IGNORED",
        "reason": "TREND_NOT_ALIGNED",
        "evidence_ref": "f" * 64,
        "fields": {"market_reason": "TREND_NOT_ALIGNED"},
        "receipt_outcome": "IGNORED",
        "receipt_reason": "TREND_NOT_ALIGNED",
        **changes,
    }


def test_only_matching_entry_events_explain_the_target_direction():
    rows = [
        record(0),
        record(0, consumer_strategy="s8", reason="UNSUPPORTED_DIRECTIONAL_EVENT"),
        record(1, event="TREND_DOWN", reason="UNSUPPORTED_DIRECTIONAL_EVENT"),
        record(2, event="ATR_EXPAND", reason="UNSUPPORTED_DIRECTIONAL_EVENT"),
        record(3, event="UNRECOGNIZED_NEW_EVENT"),
        record(4, event=None),
    ]
    result = summarize_symbol_evidence(rows)
    long = result["by_strategy"]["s6"]
    assert long["classification_version"] == CLASSIFICATION_VERSION
    assert long["candidate_count"] == 1
    assert long["reason_counts"] == [
        {"stage": "MARKET_FILTER_REJECTED", "reason": "TREND_NOT_ALIGNED", "count": 1}
    ]
    assert long["non_candidate_counts"] == {
        "OPPOSITE_DIRECTION_EVENT": 1,
        "NON_ENTRY_EVENT": 1,
        "UNKNOWN_EVENT": 2,
    }
    assert result["signal_count"] == 5
    assert result["by_strategy"]["s8"]["candidate_count"] == 0
    assert result["by_strategy"]["s8"]["rejection_details"] == []


def test_late_expiry_does_not_invent_history_maintenance_or_strategy_rejection():
    expired = record(
        action=None,
        decision_id=None,
        decided_ms=None,
        reason=None,
        evidence_ref=None,
        fields={},
        receipt_outcome="EXPIRED",
        receipt_reason="signal expired before scheduling",
        completed_ms=START + 3 * DAY_MS,
    )
    assert _lifecycle(expired) == "EXPIRED_WITHOUT_DECISION_ORIGIN_UNKNOWN"
    summary = summarize_symbol_evidence([expired])["by_strategy"]["s6"]
    assert summary["stage_counts"] == {"EXPIRED_NOT_STRATEGY_REJECTION": 1}
    assert summary["rejection_details"] == []
    assert (
        _lifecycle(
            {**expired, "fields": {"maintenance_origin": "BOUNDED_HISTORY_SWEEP"}}
        )
        == "RECORDED_EXPIRY_MAINTENANCE"
    )
    assert _lifecycle(record(action="EXPIRED")) == "RECORDED_EXPIRED_DECISION"


def test_producer_end_event_is_non_entry_not_unknown_or_rejected_opportunity():
    summary = summarize_symbol_evidence([record(event="EVENT_END")])["by_strategy"][
        "s6"
    ]
    assert summary["candidate_count"] == 0
    assert summary["non_candidate_counts"] == {"NON_ENTRY_EVENT": 1}
    assert summary["reason_counts"] == []


def test_examples_cover_each_source_window_consumer_and_a_rare_late_reason():
    rows = []
    for bucket in range(4):
        for source in ("s3", "tv_bridge", "quant"):
            for n in range(15):
                for producer in ("s6", "s8"):
                    rows.append(
                        record(
                            len(rows),
                            source=source,
                            consumer_strategy=producer,
                            observed_ms=START + bucket * SIGNAL_WINDOW_MS + n * 1000,
                        )
                    )
    rows[-1] = {**rows[-1], "reason": "RARE_LATE_REJECTION"}
    result = summarize_symbol_evidence(rows, truncated=True)
    examples = result["evidence"]
    assert len(examples) == 50
    cells = {
        (
            r["source"],
            (r["observed_ms"] - START) // SIGNAL_WINDOW_MS,
            r["consumer_strategy"],
        )
        for r in examples
    }
    assert len(cells) == 24
    assert any(r["reason"] == "RARE_LATE_REJECTION" for r in examples)
    assert result == summarize_symbol_evidence(list(reversed(rows)), truncated=True)


def test_frozen_projection_keeps_zero_false_and_source_overrides_without_secrets():
    safe = _frozen_details(
        {
            "operands": {
                "volume_ratio": "0",
                "breakout_long": False,
                "score": 0,
                "price": "NaN",
                "ema20": {"secret": "must-not-export"},
                "volume_ratio_secret": "must-not-export",
                "raw_config": "must-not-export",
                "regime": "BULL",
                "jev.chase_risk": "0.9",
            },
            "thresholds": {"entry.min_score": 70, "api_key": "must-not-export"},
            "source_thresholds": {
                "entry.min_score": 61,
                "jev.max_chase_risk": "0.7",
                "secret": 3,
            },
            "market_sources": {
                "market_environment": "LIVE",
                "market_source": "BINANCE_FUTURES",
                "api_key": "must-not-export",
            },
            "prompt": "must-not-export",
        }
    )
    assert safe["operands"]["volume_ratio"] == "0"
    assert safe["operands"]["breakout_long"] is False
    assert safe["thresholds"] == {"entry.min_score": 61, "jev.max_chase_risk": "0.7"}
    assert safe["market_sources"]["market_environment"] == "LIVE"
    assert "must-not-export" not in json.dumps(safe)
    assert _frozen_details({}) == {
        "operands": {},
        "thresholds": {},
        "market_sources": {},
    }


def test_reason_groups_preserve_counts_with_three_distributed_frozen_examples():
    rows = [
        record(
            i,
            observed_ms=START + i * SIGNAL_WINDOW_MS,
            rejection_detail={
                "operands": {"volume_ratio": str(i)},
                "thresholds": {"trend.min_volume_ratio": "1.2"},
            },
        )
        for i in range(4)
    ]
    detail = summarize_symbol_evidence(rows)["by_strategy"]["s6"]["rejection_details"][
        0
    ]
    assert detail["count"] == 4
    assert len(detail["reason_samples"]) == 3
    for sample in detail["reason_samples"]:
        assert sample["thresholds"] == {"trend.min_volume_ratio": "1.2"}
        assert sample["numeric_evidence_status"] == "RECORDED_OPERANDS_AND_THRESHOLDS"
        assert sample["market_sources"] == {}


def test_missing_event_in_legacy_examples_is_unknown_not_a_directional_candidate():
    legacy = record()
    del legacy["event"]
    result = summarize_symbol_evidence([legacy])["by_strategy"]["s6"]
    assert result["candidate_count"] == 0
    assert result["non_candidate_counts"] == {"UNKNOWN_EVENT": 1}
    assert result["reason_counts"] == []


def test_explicit_jev_timing_and_invalid_sizing_envelope_are_safe_operands():
    values = {
        "jev.started_at_ms": START,
        "jev.deadline_ms": START + 5000,
        "jev.checked_at_ms": START + 5001,
        "jev.signal_expires_at_ms": START + 5000,
        "sizing.price_high": "102",
        "sizing.price_low": "98",
        "sizing.stop_valid_through_envelope": False,
        "sizing.mark_price": "100",
    }
    result = _frozen_details({"operands": values})
    assert result["operands"] == values
    assert result["thresholds"] == {}
