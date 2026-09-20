"""Explicit S3 frame composition; no legacy runtime, network, startup or orders.

Caller supplies original observed frames, never a refreshed timestamp on retries.
Indicator comparisons retain the existing pure detector's floating-point rules;
failed-breakout prices and every durable record use decimal strings.
"""

import math
from copy import deepcopy

from s3.detector import detect_candidate_events
from services.v2_market_publishers import feature_values
from v2_core.breakout import advance_breakout
from v2_core.lifecycle import S3FrameProcessor


def numeric_features(value):
    if isinstance(value, dict):
        return {k: numeric_features(v) for k, v in value.items()}
    if isinstance(value, list):
        return [numeric_features(v) for v in value]
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return value
        if not math.isfinite(number):
            raise ValueError("nonfinite indicator")
        return number
    return value


def evaluate(contexts, raw_windows, previous, observed_at):
    states, events = deepcopy(previous), []
    for target, windows in sorted(contexts.items()):
        if not isinstance(windows, dict) or not windows:
            raise ValueError("covered symbol requires computed windows")
        numeric = numeric_features(windows)
        for period in ("15m", "1h", "4h", "24h"):
            window = numeric.get(period)
            if (
                not isinstance(window, dict)
                or type(window.get("chg")) not in (int, float)
                or not math.isfinite(window["chg"])
            ):
                raise ValueError("complete finite change windows required")
        ratio = numeric["15m"].get("vol_ratio")
        if type(ratio) not in (int, float) or not math.isfinite(ratio) or ratio < 0:
            raise ValueError("finite nonnegative volume ratio required")
        raw = raw_windows.get(target)
        if not isinstance(raw, dict) or any(
            not isinstance(raw.get(period), list)
            or not minimum <= len(raw[period]) <= 10000
            for period, minimum in (("4h", 2), ("15m", 3))
        ):
            raise ValueError("complete bounded breakout windows required")

        def breakout(symbol, raw_4h, raw_15m, output):
            state, candidates = advance_breakout(
                states.get(symbol),
                symbol=symbol,
                raw_4h=raw_4h,
                raw_15m=raw_15m,
                observed_at=observed_at,
            )
            states[symbol] = state
            output.extend(candidates)

        events.extend(
            detect_candidate_events(
                target,
                numeric,
                raw_windows.get(target, {}),
                breakout_runner=breakout,
            )
        )
    return feature_values(events), states


class S3Runtime:
    def __init__(self, publisher, **policy):
        self.processor = S3FrameProcessor(
            publisher, evaluate=evaluate, detector_version="s3-pure-v2.1", **policy
        )

    def process(
        self, *, frame_id, observed_at, windows, raw_windows, removed_symbols=()
    ):
        return self.processor.process(
            frame_id=frame_id,
            observed_at=observed_at,
            contexts=feature_values(windows),
            raw_windows=feature_values(raw_windows),
            removed_symbols=removed_symbols,
        )
