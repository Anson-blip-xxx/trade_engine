"""Closed-minute source frames -> pure windows -> durable S3 publication.

No credentials, network client, business files or implicit daemon. A source port
must supply immutable, environment-bound batches and replay until acknowledged.
"""

import math
from decimal import Decimal, InvalidOperation

from s3.core import build_window_features, ema
from services.v2_market_publishers import feature_values
from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds, symbol

WINDOWS = (("15m", 15), ("1h", 60), ("4h", 240), ("24h", 1440))
VERSION = "s3-closed-1m-v1"
PRICE_VERSION = "s3-closed-1m-v2"
ROLLING_VERSION = "s3-closed-1m-v3"
BREAKOUT_VERSION = "s3-closed-1m-v4"
CORRECTED_VERSION = "s3-closed-1m-v5"
STRUCTURE_VERSION = "s3-closed-1m-v6"


def amount(value, *, positive):
    if type(value) not in (str, int) or len(str(value)) > 80:
        raise ValueError("bounded decimal strings or integers required")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid candle number") from exc
    if (
        not number.is_finite()
        or number < 0
        or number > Decimal("1e20")
        or (positive and number == 0)
    ):
        raise ValueError("candle number outside bounds")
    # Indicator core intentionally uses floats. Reject underflow before it can
    # turn a valid positive price into a zero divisor or erase observations.
    if number and (not math.isfinite(float(number)) or float(number) == 0):
        raise ValueError("candle number outside indicator range")
    return str(number), number


def build_frame(batch, *, environment, feature_version=VERSION):
    if feature_version not in {
        VERSION,
        PRICE_VERSION,
        ROLLING_VERSION,
        BREAKOUT_VERSION,
        CORRECTED_VERSION,
        STRUCTURE_VERSION,
    }:
        raise ValueError("MARKET_FEATURE_VERSION_UNSUPPORTED")
    if environment not in ("SANDBOX", "LIVE"):
        raise ValueError("explicit environment required")
    if not isinstance(batch, dict) or set(batch) != {
        "source",
        "environment",
        "interval",
        "closed_at",
        "candles",
    }:
        raise ValueError("normalized candle batch required")
    if (
        batch["source"] != "BINANCE_FUTURES"
        or batch["environment"] != environment
        or batch["interval"] != "1m"
    ):
        raise ValueError("candle source scope mismatch")
    observed = milliseconds(batch["closed_at"])
    if observed < 1440 * 60000 or observed % 60000:
        raise ValueError("closed minute boundary required")
    candles = batch["candles"]
    if not isinstance(candles, dict) or not 1 <= len(candles) <= 100:
        raise ValueError("bounded nonempty symbol batch required")
    windows, raw = {}, {}
    total_bytes = 0
    for target, bars in sorted(candles.items()):
        symbol(target)
        if not isinstance(bars, list) or len(bars) != 1440:
            raise ValueError("full 24-hour one-minute history required")
        normalized, numeric = [], []
        for index, bar in enumerate(bars):
            if not isinstance(bar, dict) or set(bar) != {
                "t",
                "o",
                "h",
                "l",
                "c",
                "v",
                "tbv",
            }:
                raise ValueError("complete candle fields required")
            opened = milliseconds(bar["t"])
            if opened != observed - (index + 1) * 60000:
                raise ValueError(
                    "candles must be contiguous closed minutes newest first"
                )
            values = {
                key: amount(bar[key], positive=key in ("o", "h", "l", "c"))
                for key in ("o", "h", "l", "c", "v", "tbv")
            }
            prices = {key: pair[1] for key, pair in values.items()}
            if (
                not prices["l"] <= prices["o"] <= prices["h"]
                or not prices["l"] <= prices["c"] <= prices["h"]
                or prices["tbv"] > prices["v"]
            ):
                raise ValueError("inconsistent candle prices or taker volume")
            normalized.append(
                {"t": opened, **{key: pair[0] for key, pair in values.items()}}
            )
            numeric.append(
                {"t": opened, **{key: float(pair[1]) for key, pair in values.items()}}
            )
        encoded = canonical({"bars": normalized})
        total_bytes += len(encoded.encode())
        if total_bytes > 8_000_000:
            raise ValueError("candle batch exceeds input budget")
        computed = {}
        for name, count in WINDOWS:
            ascending = list(reversed(numeric[:count]))
            closes = [bar["c"] for bar in ascending]
            computed[name] = feature_values(
                build_window_features(ascending, ema(closes, 20), ema(closes, 60))
            )
            if feature_version in {PRICE_VERSION, ROLLING_VERSION, BREAKOUT_VERSION}:
                from services.v2_price_features import (
                    price_features,
                    price_features_v3,
                    price_features_v4,
                )

                computed[name].update(
                    {
                        PRICE_VERSION: price_features,
                        ROLLING_VERSION: price_features_v3,
                        BREAKOUT_VERSION: price_features_v4,
                    }[feature_version](list(reversed(normalized[:count])))
                )
            elif feature_version in {CORRECTED_VERSION, STRUCTURE_VERSION}:
                from services.v2_price_features import (
                    EMA_WARMUP_MIN_BARS,
                    price_features_v5,
                )

                computed[name].update(
                    price_features_v5(
                        list(reversed(normalized[: max(count, EMA_WARMUP_MIN_BARS)])),
                        window_bars=count,
                        closed_at=observed,
                    )
                )

            if Decimal(computed[name]["close"]) <= 0:
                raise ValueError("price below indicator output precision")
        computed["input_evidence"] = {
            "source": batch["source"],
            "environment": environment,
            "window_version": feature_version,
            "interval": "1m",
            "closed_at": observed,
            "bars_digest": digest(encoded),
            "bar_count": 1440,
        }
        if feature_version in {ROLLING_VERSION, BREAKOUT_VERSION}:
            computed["input_evidence"]["indicator_contract"] = {
                "input_interval": "1m",
                "window_semantics": "ROLLING_1M_LOOKBACK",
                "lookback_bars": dict(WINDOWS),
                "ema_period_unit": "1m_bar",
                "ema_periods": [20, 60],
                "ema_seed": "FIRST_PERIOD_SMA",
                "ema_insufficient_history": "LAST_CLOSE_LEGACY_COMPATIBILITY",
                "atr_method": "LATEST_14_TRUE_RANGES_ARITHMETIC_MEAN",
                "atr_period_unit": "1m_bar",
            }
            if feature_version == BREAKOUT_VERSION:
                computed["input_evidence"]["indicator_contract"].update(
                    close_pos_method="LATEST_CLOSE_WITHIN_ROLLING_WINDOW_HIGH_LOW_PERCENT",
                    close_pos_range=[0, 100],
                    close_pos_flat_window=50,
                )
        elif feature_version in {CORRECTED_VERSION, STRUCTURE_VERSION}:
            from services.v2_price_features import (
                ATR_EXPANSION_CONTRACT,
                EMA_WARMUP_MIN_BARS,
            )

            computed["input_evidence"]["indicator_contract"] = {
                "input_interval": "1m",
                "window_semantics": "ROLLING_1M_LOOKBACK",
                "lookback_bars": dict(WINDOWS),
                "ema_period_unit": "1m_bar",
                "ema_periods": [20, 60],
                "ema_seed": "FIRST_PERIOD_SMA",
                "ema_warmup_min_bars": EMA_WARMUP_MIN_BARS,
                "ema_input_bars": {
                    name: max(count, EMA_WARMUP_MIN_BARS) for name, count in WINDOWS
                },
                "ema_history_selection": "LATEST_EMA_INPUT_BARS_ENDING_AT_CLOSED_AT",
                "ema_insufficient_history": "REJECT",
                "atr_method": "LATEST_14_TRUE_RANGES_ARITHMETIC_MEAN",
                "atr_reference_method": "PRECEDING_NONOVERLAPPING_14_TRUE_RANGES_ARITHMETIC_MEAN",
                "atr_period_unit": "1m_bar",
                "atr_pct_denominator": "LATEST_CLOSE_FOR_CURRENT_AND_REFERENCE",
                "atr_expansion_contract": ATR_EXPANSION_CONTRACT,
                "atr_reference_zero": "NO_EXPANSION",
                "close_pos_method": "LATEST_CLOSE_WITHIN_ROLLING_WINDOW_HIGH_LOW_PERCENT",
                "close_pos_range": [0, 100],
                "close_pos_flat_window": 50,
            }
        if feature_version == STRUCTURE_VERSION:
            from v2_core.trend_structure import build_trend_structure

            computed["trend_structure"] = build_trend_structure(
                list(reversed(normalized)),
                symbol=target,
                environment=environment,
                closed_at_ms=observed,
            )
        windows[target] = computed
        # These are rolling minute windows, NOT exchange 4h/15m candles. Match
        # the existing detector's raw input convention without mislabelling it.
        raw[target] = {"15m": normalized[:15], "4h": normalized[:240]}
    return {
        "frame_id": f"{feature_version}:{observed}",
        "observed_at": observed,
        "windows": windows,
        "raw_windows": raw,
    }


class S3CandleRunner:
    """One bounded source delivery; external supervisor owns cadence/timeouts.

    Only acknowledge after PG commit AND projection success. Ack loss replays the
    same immutable source batch. No wall-clock-derived identities or memory cursor.
    """

    def __init__(self, runtime, *, source):
        self.runtime, self.source = runtime, source
        self.environment = runtime.processor.publisher.environment

    def run_once(self):
        stage = "READ"
        try:
            batch = self.source.read()
            if batch is None:
                return {"status": "IDLE"}
            stage = "VALIDATE"
            frame = build_frame(batch, environment=self.environment)
            stage = "PUBLISH"
            result = self.runtime.process(**frame)
            if result["status"] != "RECORDED" or result["market_status"] != "PROJECTED":
                return {
                    "status": "RETRY",
                    "stage": "PROJECT",
                    "frame_id": frame["frame_id"],
                }
            stage = "ACK"
            if self.source.ack(frame["frame_id"]) is not True:
                raise RuntimeError("source acknowledgement not confirmed")
            return {
                "status": "ACKNOWLEDGED",
                "frame_id": frame["frame_id"],
                "signal_ids": result["signal_ids"],
            }
        except Exception as exc:  # noqa: BLE001 - bounded diagnostics, no raw inputs or secrets
            return {"status": "RETRY", "stage": stage, "error_code": type(exc).__name__}
