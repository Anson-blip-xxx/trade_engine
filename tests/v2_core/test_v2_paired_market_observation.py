from copy import deepcopy
from decimal import Decimal, localcontext
from functools import lru_cache

import pytest

from v2_core.evidence import canonical, digest
from v2_core.paired_market_observation import compare_market_observations
from v2_core.producer import market_envelope

NOW = 86404000


def envelope(environment, **metrics):
    window = {
        "close": "100",
        "volume": "1000",
        "vol_ratio": "1",
        "taker_buy_ratio": "0.4",
        **metrics,
    }
    return market_envelope(
        "s3",
        environment,
        "BTCUSDT",
        86400000,
        {
            "15m": dict(window),
            "1h": dict(window),
            "input_evidence": {
                "source": "BINANCE_FUTURES",
                "environment": environment,
                "window_version": "s3-closed-1m-v2",
                "interval": "1m",
                "closed_at": 86400000,
                "bar_count": 1440,
                "bars_digest": digest(environment),
            },
        },
    )


def rehash(value):
    value["snapshot_id"] = digest(
        canonical({k: v for k, v in value.items() if k != "snapshot_id"})
    )
    return value


def compare(live=None, sandbox=None, **kwargs):
    return compare_market_observations(
        envelope("LIVE") if live is None else live,
        envelope("SANDBOX") if sandbox is None else sandbox,
        now_ms=NOW,
        **kwargs,
    )


@lru_cache(maxsize=12)
def generated_template(environment, version):
    from test_v2_s3_candles import batch

    from services.v2_s3_candles import build_frame

    data = batch()
    data["environment"] = environment
    frame = build_frame(data, environment=environment, feature_version=version)
    return market_envelope(
        "s3", environment, "BTCUSDT", frame["observed_at"], frame["windows"]["BTCUSDT"]
    )


def generated(environment, version):
    return deepcopy(generated_template(environment, version))


def test_exact_decimal_differences_and_non_executable_evidence():
    live, sandbox = (
        envelope("LIVE"),
        envelope(
            "SANDBOX",
            close="100.1",
            volume="1500",
            taker_buy_ratio="0.7",
            vol_ratio="1.25",
        ),
    )
    original = deepcopy((live, sandbox))
    result = compare(live, sandbox)
    assert result["status"] == "COMPARABLE"
    assert result["execution_authorized"] is result["pnl_evaluated"] is False
    assert result["difference_direction"] == "SANDBOX_MINUS_LIVE"
    for window in result["metrics"].values():
        assert Decimal(window["close_basis_bps"]["value"]) == 10
        assert Decimal(window["taker_buy_ratio_delta_pp"]["value"]) == 30
        assert Decimal(window["volume_delta"]["value"]) == 500
        assert Decimal(window["volume_ratio_sandbox_to_live"]["value"]) == Decimal(
            "1.5"
        )
        assert Decimal(window["vol_ratio_delta"]["value"]) == Decimal(".25")
    assert result["sources"]["LIVE"]["snapshot_id"] == live["snapshot_id"]
    assert result["sources"]["LIVE"]["envelope_digest_verified"] is True
    assert result["sources"]["LIVE"]["raw_candle_digest_verified"] is False
    result["sources"]["LIVE"]["input_evidence"]["environment"] = "changed"
    assert (live, sandbox) == original


def test_zero_flow_ratio_is_not_missing_and_zero_volume_is_not_flow_evidence():
    result = compare(envelope("LIVE", taker_buy_ratio="0"))
    assert Decimal(result["metrics"]["15m"]["taker_buy_ratio_delta_pp"]["value"]) == 40
    result = compare(envelope("LIVE", volume="0", taker_buy_ratio="0.5"))
    assert result["status"] == "PARTIAL"
    window = result["metrics"]["15m"]
    assert window["taker_buy_ratio_delta_pp"]["reason"] == "ZERO_OBSERVED_VOLUME"
    assert window["vol_ratio_delta"]["reason"] == "ZERO_OBSERVED_VOLUME"
    assert window["volume_ratio_sandbox_to_live"]["reason"] == "ZERO_LIVE_DENOMINATOR"
    assert Decimal(window["volume_delta"]["value"]) == 1000


@pytest.mark.parametrize(
    "bad", [None, "NaN", "Infinity", "-0.1", "1.01", True, "1e-999999"]
)
def test_unknown_or_invalid_ratios_never_become_zero(bad):
    result = compare(envelope("LIVE", taker_buy_ratio=bad))
    assert result["status"] == "PARTIAL"
    assert result["metrics"]["15m"]["taker_buy_ratio_delta_pp"]["status"] == "UNKNOWN"
    assert "value" not in result["metrics"]["15m"]["taker_buy_ratio_delta_pp"]


@pytest.mark.parametrize(
    "key,bad",
    [("close", "0"), ("volume", "-1"), ("vol_ratio", "bad"), ("close", "1e21")],
)
def test_invalid_metrics_do_not_authorize_execution(key, bad):
    result = compare(envelope("LIVE", **{key: bad}))
    assert result["status"] == "PARTIAL"
    assert result["execution_authorized"] is False


def test_missing_observation_and_metric_are_explicit_unknowns():
    result = compare_market_observations(None, envelope("SANDBOX"), now_ms=NOW)
    assert result["status"] == "UNKNOWN"
    assert result["sources"]["LIVE"]["reason"] == "MISSING_OBSERVATION"
    live = envelope("LIVE")
    del live["features"]["15m"]["taker_buy_ratio"]
    result = compare(rehash(live))
    assert result["status"] == "PARTIAL"
    assert (
        result["metrics"]["15m"]["taker_buy_ratio_delta_pp"]["reason"]
        == "MISSING_METRIC"
    )


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda v: v.update(environment="SANDBOX"), "SOURCE_SCOPE_MISMATCH"),
        (lambda v: v.update(source="s0"), "SOURCE_SCOPE_MISMATCH"),
        (
            lambda v: v["features"]["input_evidence"].update(environment="SANDBOX"),
            "INVALID_INPUT_EVIDENCE",
        ),
        (
            lambda v: v["features"]["input_evidence"].update(closed_at=86400001),
            "INVALID_INPUT_EVIDENCE",
        ),
        (
            lambda v: v["features"]["input_evidence"].update(bar_count=True),
            "INVALID_INPUT_EVIDENCE",
        ),
        (
            lambda v: v["features"]["input_evidence"].update(bars_digest="forged"),
            "INVALID_INPUT_EVIDENCE",
        ),
        (lambda v: v["features"].pop("1h"), "MISSING_FEATURE_WINDOW"),
    ],
)
def test_source_contract_cannot_be_relabelled_even_with_recomputed_envelope_hash(
    mutation, reason
):
    live = envelope("LIVE")
    mutation(live)
    result = compare(rehash(live))
    assert result["status"] == "UNKNOWN"
    assert result["sources"]["LIVE"]["reason"] == reason
    assert result["metrics"] == {}


def test_tampering_and_float_payloads_are_rejected():
    live = envelope("LIVE")
    live["features"]["15m"]["close"] = "999"
    assert compare(live)["sources"]["LIVE"]["reason"] == "ENVELOPE_DIGEST_MISMATCH"
    live["features"]["15m"]["close"] = 1.5
    assert compare(live)["sources"]["LIVE"]["reason"] == "INVALID_ENVELOPE"


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("symbol", "SYMBOL_MISMATCH"),
        ("time", "CLOSED_AT_MISMATCH"),
        ("version", "FEATURE_VERSION_MISMATCH"),
    ],
)
def test_only_same_symbol_cutoff_and_feature_calculation_can_be_compared(
    defect, reason
):
    sandbox = envelope("SANDBOX")
    if defect == "symbol":
        sandbox["symbol"] = "ETHUSDT"
    elif defect == "time":
        sandbox["observed_at"] -= 60000
        sandbox["features"]["input_evidence"]["closed_at"] -= 60000
        live = envelope("LIVE")
        # Both remain at least a complete day and within the freshness limit.
        for value in (live, sandbox):
            value["observed_at"] += 60000
            value["features"]["input_evidence"]["closed_at"] += 60000
        result = compare_market_observations(
            rehash(live), rehash(sandbox), now_ms=NOW + 60000
        )
        assert result["reason"] == reason
        return
    elif defect == "version":
        sandbox["features"]["input_evidence"]["window_version"] = "s3-closed-1m-v1"
    result = compare(sandbox=rehash(sandbox))
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == reason


def test_observation_age_boundary_and_future_rejected_without_timestamp_refresh():
    live, sandbox = envelope("LIVE"), envelope("SANDBOX")
    assert (
        compare_market_observations(live, sandbox, now_ms=86490000)["status"]
        == "COMPARABLE"
    )
    assert (
        compare_market_observations(live, sandbox, now_ms=86490001)["sources"]["LIVE"][
            "reason"
        ]
        == "STALE_OBSERVATION"
    )
    assert (
        compare_market_observations(live, sandbox, now_ms=86399999)["sources"]["LIVE"][
            "reason"
        ]
        == "FUTURE_OBSERVATION"
    )


@pytest.mark.parametrize("maximum", [0, -1, True, 86400001, "90000"])
def test_invalid_freshness_configuration_rejected(maximum):
    with pytest.raises(ValueError, match="FRESHNESS"):
        compare(max_age_ms=maximum)


def test_decimal_result_does_not_depend_on_callers_precision():
    with localcontext() as context:
        context.prec = 3
        result = compare(
            envelope("LIVE", close="123.456789"),
            envelope("SANDBOX", close="123.456790"),
        )
    with localcontext() as context:
        context.prec = 50
        expected = (
            (Decimal("123.456790") - Decimal("123.456789"))
            / Decimal("123.456789")
            * 10000
        )
    assert Decimal(result["metrics"]["15m"]["close_basis_bps"]["value"]) == expected


@pytest.mark.parametrize("version", [f"s3-closed-1m-v{i}" for i in range(1, 7)])
def test_real_build_frame_evidence_shape_is_supported(version):
    observations = [
        generated(environment, version) for environment in ("LIVE", "SANDBOX")
    ]
    result = compare_market_observations(*observations, now_ms=NOW)
    assert result["status"] == "COMPARABLE"
    assert result["feature_version"] == version
    assert result["metrics"]["1h"]["taker_buy_ratio_delta_pp"]["value"] == "0"


@pytest.mark.parametrize("version", [f"s3-closed-1m-v{i}" for i in range(3, 7)])
@pytest.mark.parametrize(
    "defect",
    ["missing", "null", "empty", "wrong_value", "missing_field", "extra_field"],
)
def test_both_rehashed_equal_but_invalid_indicator_contracts_are_unknown(
    version, defect
):
    observations = [
        generated(environment, version) for environment in ("LIVE", "SANDBOX")
    ]
    for value in observations:
        evidence = value["features"]["input_evidence"]
        if defect == "missing":
            del evidence["indicator_contract"]
        elif defect == "null":
            evidence["indicator_contract"] = None
        elif defect == "empty":
            evidence["indicator_contract"] = {}
        elif defect == "wrong_value":
            evidence["indicator_contract"]["ema_period_unit"] = "15m_bar"
        elif defect == "missing_field":
            del evidence["indicator_contract"]["ema_seed"]
        else:
            evidence["indicator_contract"]["undocumented_default"] = True
        rehash(value)
    result = compare_market_observations(*observations, now_ms=NOW)
    assert result["status"] == "UNKNOWN" and result["metrics"] == {}
    assert all(
        proof["reason"] == "INVALID_INDICATOR_CONTRACT"
        for proof in result["sources"].values()
    )
    assert result["execution_authorized"] is False


@pytest.mark.parametrize("version", [f"s3-closed-1m-v{i}" for i in (4, 5, 6)])
def test_contract_boolean_is_not_a_valid_numeric_zero(version):
    live, sandbox = (
        generated(environment, version) for environment in ("LIVE", "SANDBOX")
    )
    live["features"]["input_evidence"]["indicator_contract"]["close_pos_range"] = [
        False,
        100,
    ]
    result = compare(rehash(live), sandbox)
    assert result["sources"]["LIVE"]["reason"] == "INVALID_INDICATOR_CONTRACT"


@pytest.mark.parametrize("version", ["s3-closed-1m-v1", "s3-closed-1m-v2"])
@pytest.mark.parametrize("contract", [None, {}, {"unknown": "method"}])
def test_legacy_contract_must_be_absent_not_fabricated(version, contract):
    live, sandbox = (
        generated(environment, version) for environment in ("LIVE", "SANDBOX")
    )
    live["features"]["input_evidence"]["indicator_contract"] = contract
    result = compare(rehash(live), sandbox)
    assert result["sources"]["LIVE"]["reason"] == "INVALID_INDICATOR_CONTRACT"


@pytest.mark.parametrize("environment", ["LIVE", "SANDBOX"])
@pytest.mark.parametrize("volume", [None, "0", "-1", "NaN", True, "missing"])
def test_flow_and_volume_ratio_need_nonzero_observed_volume(environment, volume):
    live, sandbox = envelope("LIVE"), envelope("SANDBOX")
    selected = live if environment == "LIVE" else sandbox
    for period in ("15m", "1h"):
        if volume == "missing":
            del selected["features"][period]["volume"]
        else:
            selected["features"][period]["volume"] = volume
    result = compare(rehash(live), rehash(sandbox))
    assert result["status"] == "PARTIAL"
    for metrics in result["metrics"].values():
        assert metrics["close_basis_bps"]["status"] == "COMPARABLE"
        for key in ("taker_buy_ratio_delta_pp", "vol_ratio_delta"):
            assert metrics[key]["status"] == "UNKNOWN"
            assert "value" not in metrics[key]
