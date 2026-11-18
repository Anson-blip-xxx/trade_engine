"""Pure, non-executable comparison of frozen LIVE and SANDBOX S3 observations.

Envelope hashes prove content consistency, not exchange authenticity. The raw
candles are not supplied here: their original digest is retained, not claimed
to have been independently verified. No trade result or execution permission is
derived from a difference between markets.
"""

import re
from copy import deepcopy
from decimal import Context, Decimal, InvalidOperation, localcontext

from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds, symbol

VERSION = "paired-public-market-observation-v1"
_ENVELOPE_FIELDS = {
    "snapshot_id",
    "source",
    "environment",
    "symbol",
    "observed_at",
    "features",
}
_VERSIONS = {f"s3-closed-1m-v{i}" for i in range(1, 7)}
_HASH = re.compile(r"[0-9a-f]{64}")
_MISSING = object()


def _unknown(reason):
    return {"status": "UNKNOWN", "reason": reason}


def _indicator_contract(version):
    """Versioned producer contract, not a claim that candle math was replayed.

    These values are frozen by services.v2_s3_candles.build_frame. Its generated
    contracts are exercised independently in this module's compatibility tests.
    A future algorithm must receive a new supported version, not silently relax
    this comparison contract.
    """
    if version in {"s3-closed-1m-v1", "s3-closed-1m-v2"}:
        return None
    contract = {
        "input_interval": "1m",
        "window_semantics": "ROLLING_1M_LOOKBACK",
        "lookback_bars": {"15m": 15, "1h": 60, "4h": 240, "24h": 1440},
        "ema_period_unit": "1m_bar",
        "ema_periods": [20, 60],
        "ema_seed": "FIRST_PERIOD_SMA",
        "ema_insufficient_history": "LAST_CLOSE_LEGACY_COMPATIBILITY",
        "atr_method": "LATEST_14_TRUE_RANGES_ARITHMETIC_MEAN",
        "atr_period_unit": "1m_bar",
    }
    if version != "s3-closed-1m-v3":
        contract.update(
            close_pos_method="LATEST_CLOSE_WITHIN_ROLLING_WINDOW_HIGH_LOW_PERCENT",
            close_pos_range=[0, 100],
            close_pos_flat_window=50,
        )
    if version in {"s3-closed-1m-v5", "s3-closed-1m-v6"}:
        contract.update(
            ema_warmup_min_bars=120,
            ema_input_bars={"15m": 120, "1h": 120, "4h": 240, "24h": 1440},
            ema_history_selection="LATEST_EMA_INPUT_BARS_ENDING_AT_CLOSED_AT",
            ema_insufficient_history="REJECT",
            atr_reference_method="PRECEDING_NONOVERLAPPING_14_TRUE_RANGES_ARITHMETIC_MEAN",
            atr_pct_denominator="LATEST_CLOSE_FOR_CURRENT_AND_REFERENCE",
            atr_expansion_contract="LATEST_14_VS_PRECEDING_14_TR_1M",
            atr_reference_zero="NO_EXPANSION",
        )
    return contract


def _valid_indicator_contract(evidence):
    expected = _indicator_contract(evidence["window_version"])
    if expected is None:
        # V1/V2 did not emit this field. A supplied null/empty/unknown contract
        # is not proof of a legacy calculation and must not be ignored.
        return "indicator_contract" not in evidence
    actual = evidence.get("indicator_contract")
    return isinstance(actual, dict) and canonical({"contract": actual}) == canonical(
        {"contract": expected}
    )


def _source(value, environment, now, max_age):
    if value is None:
        return None, _unknown("MISSING_OBSERVATION")
    if not isinstance(value, dict) or set(value) != _ENVELOPE_FIELDS:
        return None, _unknown("INVALID_ENVELOPE")
    try:
        encoded = canonical({k: v for k, v in value.items() if k != "snapshot_id"})
        if len(encoded.encode()) > 1_000_000:
            return None, _unknown("OVERSIZED_ENVELOPE")
        if value["snapshot_id"] != digest(encoded):
            return None, _unknown("ENVELOPE_DIGEST_MISMATCH")
        if value["source"] != "s3" or value["environment"] != environment:
            return None, _unknown("SOURCE_SCOPE_MISMATCH")
        symbol(value["symbol"])
        observed = milliseconds(value["observed_at"])
        if observed > now:
            return None, _unknown("FUTURE_OBSERVATION")
        if now - observed > max_age:
            return None, _unknown("STALE_OBSERVATION")
        features = value["features"]
        if not isinstance(features, dict):
            return None, _unknown("INVALID_FEATURES")
        evidence = features.get("input_evidence")
        if (
            not isinstance(evidence, dict)
            or evidence.get("source") != "BINANCE_FUTURES"
            or evidence.get("environment") != environment
            or evidence.get("interval") != "1m"
            or evidence.get("window_version") not in _VERSIONS
            or type(evidence.get("closed_at")) is not int
            or evidence["closed_at"] != observed
            or observed < 86400000
            or observed % 60000
            or type(evidence.get("bar_count")) is not int
            or evidence["bar_count"] != 1440
            or not isinstance(evidence.get("bars_digest"), str)
            or not _HASH.fullmatch(evidence["bars_digest"])
        ):
            return None, _unknown("INVALID_INPUT_EVIDENCE")
        if not _valid_indicator_contract(evidence):
            return None, _unknown("INVALID_INDICATOR_CONTRACT")
        if any(not isinstance(features.get(period), dict) for period in ("15m", "1h")):
            return None, _unknown("MISSING_FEATURE_WINDOW")
    except (ValueError, TypeError, KeyError, RecursionError):
        return None, _unknown("INVALID_ENVELOPE")
    return value, {
        "status": "VALID",
        "environment": environment,
        "symbol": value["symbol"],
        "snapshot_id": value["snapshot_id"],
        "observed_at_ms": observed,
        "input_evidence": deepcopy(evidence),
        "envelope_digest_verified": True,
        "raw_candle_digest_verified": False,
    }


def _number(value, *, positive=False, ratio=False):
    if value is _MISSING or value is None:
        return None, "MISSING_METRIC"
    if type(value) not in (str, int) or len(str(value)) > 80:
        return None, "INVALID_METRIC"
    try:
        number = Decimal(value)
        if (
            not number.is_finite()
            or number < 0
            or number > Decimal("1e20")
            or (number and number.adjusted() < -100)
            or (positive and number == 0)
            or (ratio and number > 1)
        ):
            return None, "INVALID_METRIC"
    except (ValueError, InvalidOperation):
        return None, "INVALID_METRIC"
    return number, None


def _metric(live, sandbox, key, *, operation="difference", positive=False, ratio=False):
    left, left_error = _number(live.get(key, _MISSING), positive=positive, ratio=ratio)
    right, right_error = _number(
        sandbox.get(key, _MISSING), positive=positive, ratio=ratio
    )
    if left_error or right_error:
        return {
            **_unknown(left_error or right_error),
            "source_status": {
                "LIVE": left_error or "VALID",
                "SANDBOX": right_error or "VALID",
            },
        }
    # Producer defaults at zero volume (flow=0.5 and vol_ratio=1) are not
    # observations. Neither derived ratio is comparable without real volume.
    if operation == "flow_pp" or key == "vol_ratio":
        for window in (live, sandbox):
            volume, error = _number(window.get("volume", _MISSING))
            if error or volume == 0:
                return _unknown(error or "ZERO_OBSERVED_VOLUME")
    with localcontext(Context(prec=50)):
        if operation == "basis_bps":
            result = (right - left) / left * 10000
        elif operation == "ratio":
            if left == 0:
                return _unknown("ZERO_LIVE_DENOMINATOR")
            result = right / left
        elif operation == "flow_pp":
            result = (right - left) * 100
        else:
            result = right - left
        return {
            "status": "COMPARABLE",
            "live": str(left.normalize()),
            "sandbox": str(right.normalize()),
            "value": str(result.normalize()) if result else "0",
        }


def compare_market_observations(live, sandbox, *, now_ms, max_age_ms=90000):
    """Compare only aligned, fresh observations; no I/O and no implicit clock.

    All differences are SANDBOX minus LIVE. Price basis is divided by LIVE and
    expressed in basis points; flow is in percentage points, not percent change.
    Missing or malformed data is UNKNOWN, never an assumed neutral value.
    """
    milliseconds(now_ms)
    if type(max_age_ms) is not int or not 1 <= max_age_ms <= 86400000:
        raise ValueError("BOUNDED_MARKET_COMPARISON_FRESHNESS_REQUIRED")
    original_live, original_sandbox = live, sandbox
    live, live_proof = _source(original_live, "LIVE", now_ms, max_age_ms)
    sandbox, sandbox_proof = _source(original_sandbox, "SANDBOX", now_ms, max_age_ms)
    report = {
        "version": VERSION,
        "evaluated_at_ms": now_ms,
        "max_age_ms": max_age_ms,
        "execution_authorized": False,
        "pnl_evaluated": False,
        "difference_direction": "SANDBOX_MINUS_LIVE",
        "sources": {"LIVE": live_proof, "SANDBOX": sandbox_proof},
        "metrics": {},
    }
    if live is None or sandbox is None:
        return {**report, **_unknown("SOURCE_UNAVAILABLE")}
    for key, reason in (
        ("symbol", "SYMBOL_MISMATCH"),
        ("observed_at", "CLOSED_AT_MISMATCH"),
    ):
        if live[key] != sandbox[key]:
            return {**report, **_unknown(reason)}
    left_evidence, right_evidence = (
        value["features"]["input_evidence"] for value in (live, sandbox)
    )
    if left_evidence["window_version"] != right_evidence["window_version"]:
        return {**report, **_unknown("FEATURE_VERSION_MISMATCH")}
    # A changed indicator contract is a different calculation even when its
    # version label was accidentally left unchanged.
    if left_evidence.get("indicator_contract") != right_evidence.get(
        "indicator_contract"
    ):
        return {**report, **_unknown("INDICATOR_CONTRACT_MISMATCH")}
    for period in ("15m", "1h"):
        left, right = live["features"][period], sandbox["features"][period]
        report["metrics"][period] = {
            "close_basis_bps": _metric(
                left, right, "close", operation="basis_bps", positive=True
            ),
            "taker_buy_ratio_delta_pp": _metric(
                left, right, "taker_buy_ratio", operation="flow_pp", ratio=True
            ),
            "volume_delta": _metric(left, right, "volume"),
            "volume_ratio_sandbox_to_live": _metric(
                left, right, "volume", operation="ratio"
            ),
            "vol_ratio_delta": _metric(left, right, "vol_ratio"),
        }
    complete = all(
        metric["status"] == "COMPARABLE"
        for window in report["metrics"].values()
        for metric in window.values()
    )
    return {
        **report,
        "status": "COMPARABLE" if complete else "PARTIAL",
        "symbol": live["symbol"],
        "closed_at_ms": live["observed_at"],
        "feature_version": left_evidence["window_version"],
    }
