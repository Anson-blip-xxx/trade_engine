import math
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import ROUND_UP, Inexact, Rounded, localcontext

import pytest

from v2_core.quant_indicators import Bar
from v2_core.regime_filter import (
    FEATURE_VERSION,
    GaussianRegimeModel,
    binary_calibration,
    causal_features,
    forward_filter,
)


def model(**changes):
    return GaussianRegimeModel(
        **{
            "model_id": "qa-frozen-model",
            "training_end_ms": 60000,
            "available_at_ms": 60000,
            "training_digest": "a" * 64,
            "scaler_training_digest": "a" * 64,
            "scaler_fit_end_ms": 60000,
            "timeframe_minutes": 1,
            "lookback": 20,
            "start": (0.5, 0.5),
            "transition": ((0.9, 0.1), (0.2, 0.8)),
            "means": ((0, 0, 0, 0, 0), (1, 1, 1, 1, 1)),
            "variances": ((1, 1, 1, 1, 1), (1, 1, 1, 1, 1)),
            "scaler_mean": (0, 0, 0, 0, 0),
            "scaler_scale": (1, 1, 1, 1, 1),
            **changes,
        }
    )


def observations(count=4):
    return [
        {
            "closed_at_ms": (i + 2) * 60000,
            "values": [0.1 * i] * 5,
            "feature_version": FEATURE_VERSION,
            "timeframe_minutes": 1,
            "lookback": 20,
        }
        for i in range(count)
    ]


def run(rows, **changes):
    return forward_filter(
        model(),
        rows,
        **{
            "timeframe_minutes": 1,
            "as_of_ms": rows[-1]["closed_at_ms"],
            "max_age_ms": 60000,
            **changes,
        },
    )


def test_future_changes_cannot_change_earlier_filtered_probabilities():
    original = observations()
    changed = deepcopy(original)
    changed[-1]["values"] = [1000] * 5
    short = run(original[:2])
    assert run(original)["observations"][:2] == short["observations"]
    assert run(changed)["observations"][:3] == run(original)["observations"][:3]
    assert run(changed)["observations"][-1] != run(original)["observations"][-1]


def test_normalized_forward_and_predictive_probabilities_not_trading_authority():
    report = run(observations())
    assert report["execution_authorized"] is False
    assert report["status"] == "RESEARCH_ONLY"
    assert len(report["model_digest"]) == 64
    for row in report["observations"]:
        assert sum(row["filtered_probabilities"]) == pytest.approx(1)
        assert sum(row["next_state_probabilities"]) == pytest.approx(1)
        assert row["expected_duration_bars"] > 0
        assert row["duration_status"] == "FINITE_GEOMETRIC"
        assert math.isfinite(row["scaled_log_predictive_density"])
        assert "log_predictive_density" not in row


def test_log_filter_can_recover_state_whose_displayed_probability_underflows():
    selected = model(
        transition=((1, 0), (0, 1)),
        means=((0, 0, 0, 0, 0), (50, 50, 50, 50, 50)),
    )
    rows = observations(2)
    rows[0]["values"], rows[1]["values"] = [0] * 5, [50] * 5
    report = forward_filter(
        selected, rows, timeframe_minutes=1, as_of_ms=180000, max_age_ms=60000
    )
    assert report["observations"][0]["filtered_probabilities"][1] == 0
    assert report["observations"][1]["filtered_probabilities"] == pytest.approx(
        [0.5, 0.5]
    )
    assert report["observations"][0]["duration_status"] == "INFINITE_ABSORBING"
    assert report["observations"][0]["expected_duration_bars"] is None


def test_extreme_finite_density_stays_normalized_and_preserves_unequal_prior():
    selected = model(start=(0.8, 0.2))
    rows = observations(2)
    for row in rows:
        row["values"] = [1e50] * 5
    report = forward_filter(
        selected, rows, timeframe_minutes=1, as_of_ms=180000, max_age_ms=60000
    )
    assert report["observations"][0]["filtered_probabilities"] == pytest.approx(
        [0.8, 0.2]
    )
    assert report["observations"][1]["filtered_probabilities"] == pytest.approx(
        report["observations"][0]["next_state_probabilities"]
    )
    for row in report["observations"]:
        for name in ("filtered_probabilities", "next_state_probabilities"):
            assert all(math.isfinite(p) and 0 <= p <= 1 for p in row[name])
            assert math.fsum(row[name]) == pytest.approx(1)


def test_forward_probabilities_and_scaled_density_match_direct_enumeration():
    selected = model()
    rows = observations(2)
    report = run(rows)
    previous = selected.start
    for index, observation in enumerate(rows):
        prior = (
            previous
            if index == 0
            else [
                math.fsum(previous[j] * selected.transition[j][k] for j in range(2))
                for k in range(2)
            ]
        )
        weights = [
            prior[k]
            * math.prod(
                math.exp(-0.5 * (value - mean) ** 2) / math.sqrt(2 * math.pi)
                for value, mean in zip(
                    observation["values"], selected.means[k], strict=True
                )
            )
            for k in range(2)
        ]
        density = math.fsum(weights)
        expected = [weight / density for weight in weights]
        assert report["observations"][index]["filtered_probabilities"] == pytest.approx(
            expected
        )
        assert report["observations"][index][
            "scaled_log_predictive_density"
        ] == pytest.approx(math.log(density))
        previous = expected


def test_large_density_cannot_revive_structurally_unreachable_state():
    selected = model(
        start=(1, 0),
        transition=((1, 0), (0, 1)),
        means=((0,) * 5, (1e50,) * 5),
    )
    rows = observations(1)
    rows[0]["values"] = [1e50] * 5
    result = forward_filter(
        selected, rows, timeframe_minutes=1, as_of_ms=120000, max_age_ms=60000
    )
    assert result["observations"][0]["filtered_probabilities"] == [1, 0]
    assert result["observations"][0]["next_state_probabilities"] == [1, 0]


@pytest.mark.parametrize(
    "value", [1e200, 10**400, math.nan, math.inf, -math.inf, True, "0"]
)
def test_derived_numeric_overflow_is_explicit_not_an_accepted_model(value):
    rows = observations()
    rows[0]["values"][0] = value
    with pytest.raises(ValueError, match="REGIME_"):
        run(rows)


@pytest.mark.parametrize(
    "changes",
    [
        {"start": (0.4, 0.4)},
        {"start": (1e308, 1e308)},
        {"start": (True, 0)},
        {"transition": ((1, 0), (1, float("nan")))},
        {"variances": ((0, 1, 1, 1, 1), (1, 1, 1, 1, 1))},
        {"scaler_scale": (0, 1, 1, 1, 1)},
        {"scaler_mean": (True, 0, 0, 0, 0)},
        {"available_at_ms": 0},
        {"training_end_ms": True},
        {"training_digest": "not-a-digest"},
        {"training_digest": "A" * 64},
        {"scaler_training_digest": "b" * 64},
        {"scaler_training_digest": None},
        {"scaler_fit_end_ms": 60001},
        {"scaler_fit_end_ms": -1},
        {"scaler_fit_end_ms": True},
        {"scaler_fit_end_ms": "60000"},
        {"scaler_fit_end_ms": math.nan},
        {"available_at_ms": 2**53},
        {"timeframe_minutes": True},
        {"timeframe_minutes": 2},
        {"lookback": True},
        {"lookback": 1},
        {"lookback": 201},
    ],
)
def test_invalid_model_never_outputs_probabilities(changes):
    with pytest.raises(ValueError, match="REGIME_"):
        model(**changes)


@pytest.mark.parametrize(
    "field",
    ["scaler_training_digest", "scaler_fit_end_ms", "timeframe_minutes", "lookback"],
)
def test_model_requires_scaler_provenance_and_feature_specification(field):
    payload = asdict(model())
    del payload[field]
    with pytest.raises(TypeError):
        GaussianRegimeModel(**payload)


def test_model_digest_binds_scaler_provenance_and_feature_specification():
    original = model()
    variants = [
        replace(original, scaler_fit_end_ms=0),
        replace(original, timeframe_minutes=5),
        replace(original, lookback=21),
        replace(original, training_digest="b" * 64, scaler_training_digest="b" * 64),
    ]
    assert len({original.fingerprint, *(m.fingerprint for m in variants)}) == 5
    assert model(lookback=2).lookback == 2
    assert model(lookback=200).lookback == 200
    report = run(observations())
    assert report["training_digest"] == report["scaler_training_digest"] == "a" * 64
    assert report["scaler_fit_end_ms"] <= report["training_end_ms"]
    assert report["feature_version"] == FEATURE_VERSION
    assert report["timeframe_minutes"] == 1
    assert report["lookback"] == 20


@pytest.mark.parametrize("defect", ["gap", "future", "pre_model", "nan", "size"])
def test_invalid_inference_data_fails_explicitly(defect):
    rows = observations()
    as_of = rows[-1]["closed_at_ms"]
    if defect == "gap":
        rows[1]["closed_at_ms"] += 60000
    elif defect == "future":
        as_of -= 1
    elif defect == "pre_model":
        rows[0]["closed_at_ms"] = 60000
    elif defect == "nan":
        rows[0]["values"][0] = float("nan")
    else:
        rows[0]["values"] = [1]
    with pytest.raises(ValueError, match="REGIME_"):
        run(rows, as_of_ms=as_of)


@pytest.mark.parametrize(
    "changes",
    [
        {"feature_version": "same-dimensions-different-features"},
        {"feature_version": None},
        {"timeframe_minutes": 5},
        {"timeframe_minutes": True},
        {"lookback": 21},
        {"lookback": 20.0},
    ],
)
def test_observations_must_match_frozen_model_feature_specification(changes):
    rows = observations()
    rows[0].update(changes)
    with pytest.raises(ValueError, match="REGIME_OBSERVATION_FEATURE_CONTRACT"):
        run(rows)


@pytest.mark.parametrize("field", ["feature_version", "timeframe_minutes", "lookback"])
def test_observations_cannot_omit_feature_specification(field):
    rows = observations()
    del rows[0][field]
    with pytest.raises(ValueError, match="REGIME_OBSERVATION_SHAPE"):
        run(rows)


@pytest.mark.parametrize(
    "changes",
    [
        {"timeframe_minutes": 5},
        {"as_of_ms": -1},
        {"as_of_ms": 2**53},
        {"as_of_ms": True},
        {"max_age_ms": 0},
        {"max_age_ms": 86400001},
        {"max_age_ms": True},
    ],
)
def test_invalid_filter_bounds_fail_explicitly(changes):
    with pytest.raises(ValueError, match="REGIME_FILTER_BOUNDS"):
        run(observations(), **changes)


def test_model_unavailable_at_bar_start_cannot_be_used_retroactively():
    with pytest.raises(ValueError, match="OBSERVATION_TIME"):
        forward_filter(
            replace(model(), available_at_ms=60001),
            observations(),
            timeframe_minutes=1,
            as_of_ms=300000,
            max_age_ms=60000,
        )


def test_stale_output_never_implies_authority():
    assert run(observations(), as_of_ms=359999)["status"] == "RESEARCH_ONLY"
    result = run(observations(), as_of_ms=360000)
    assert result["status"] == "STALE"
    assert result["execution_authorized"] is False


def bars(count=40):
    return [
        Bar(
            t=i * 60000,
            o=str(100 + i),
            h=str(102 + i),
            l=str(99 + i),
            c=str(101 + i),
            v=str(1000 + i),
        )
        for i in range(count)
    ]


def test_feature_prefix_invariance_and_unclosed_bar_exclusion():
    original = bars()
    result = causal_features(original, timeframe_minutes=1, as_of_ms=35 * 60000)
    changed = original[:35] + [
        Bar(t=b.t, o="1", h="100000", l="0.1", c="90000", v="999999")
        for b in original[35:]
    ]
    assert causal_features(changed, timeframe_minutes=1, as_of_ms=35 * 60000) == result
    full = causal_features(original, timeframe_minutes=1, as_of_ms=40 * 60000)
    assert full[: len(result)] == result
    assert all(r["closed_at_ms"] <= 35 * 60000 for r in result)


@pytest.mark.parametrize("defect", ["gap", "misalignment", "duplicate", "out_of_order"])
def test_future_history_defects_do_not_change_past_asof_features(defect):
    original = bars()
    changed = original[:]
    if defect == "gap":
        del changed[37]
    elif defect == "misalignment":
        changed[37] = replace(changed[37], t=changed[37].t + 1)
    elif defect == "duplicate":
        changed[38] = changed[37]
    else:
        changed[37], changed[38] = changed[38], changed[37]
    kwargs = {"timeframe_minutes": 1, "as_of_ms": 35 * 60000}
    assert causal_features(changed, **kwargs) == causal_features(original, **kwargs)
    with pytest.raises(ValueError, match="REGIME_FEATURE_GAP"):
        causal_features(changed, timeframe_minutes=1, as_of_ms=41 * 60000)


def test_feature_decimal_arithmetic_is_independent_of_ambient_context():
    original = bars()
    kwargs = {"timeframe_minutes": 1, "as_of_ms": 40 * 60000}
    expected = causal_features(original, **kwargs)
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_UP
        context.Emin, context.Emax = -1, 1
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        assert causal_features(original, **kwargs) == expected
        assert context.prec == 3
        assert context.traps[Inexact] is True


def test_causal_feature_rows_bind_version_timeframe_and_lookback_for_filter():
    rows = causal_features(
        bars(), timeframe_minutes=1, as_of_ms=40 * 60000, lookback=20
    )
    for row in rows:
        assert row["feature_version"] == "closed-ohlcv-regime-v1"
        assert row["timeframe_minutes"] == 1
        assert row["lookback"] == 20
    report = run(rows)
    assert report["status"] == "RESEARCH_ONLY"
    assert len(report["observations"]) == len(rows)
    mismatched = causal_features(
        bars(), timeframe_minutes=1, as_of_ms=40 * 60000, lookback=21
    )
    with pytest.raises(ValueError, match="REGIME_OBSERVATION_FEATURE_CONTRACT"):
        run(mismatched)


def test_calibration_scores_observed_event_and_preserves_empty_bins():
    report = binary_calibration([0, 0.5, 1], [0, 1, 1])
    assert report["brier_score"] == pytest.approx(1 / 12)
    assert report["reliability"][-1]["count"] == 1
    assert report["reliability"][1]["event_frequency"] is None
    assert report["execution_authorized"] is False
    with pytest.raises(ValueError, match="EVENT_REQUIRED"):
        binary_calibration([0.2], ["bull_market"])
