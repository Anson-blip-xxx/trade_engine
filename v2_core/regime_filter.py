"""Causal Gaussian-HMM inference, not a fitted model or permission to trade.

Only forward filtering is implemented. Full-sample Viterbi/smoothed posterior
labels must never be supplied as historical entry evidence. Model fitting and
out-of-sample acceptance are deliberately separate from execution.
"""

import math
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Context, localcontext

from v2_core.evidence import canonical, digest

VERSION = "causal-regime-filter-v1"
FEATURE_VERSION = "closed-ohlcv-regime-v1"
TIMEFRAMES = (1, 5, 15, 30, 60, 240, 1440)
MAX_TIMESTAMP_MS = 2**53 - 1
FEATURES = (
    "log_return",
    "realized_volatility",
    "range_fraction",
    "volume_ratio",
    "trend",
)


def _finite(value):
    if type(value) not in (int, float):
        raise ValueError("REGIME_FINITE_NUMBER_REQUIRED")
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise ValueError("REGIME_FINITE_NUMBER_REQUIRED") from None
    if not math.isfinite(value):
        raise ValueError("REGIME_FINITE_NUMBER_REQUIRED")
    return value


def _logsumexp(values):
    peak = max(values)
    return (
        -math.inf
        if peak == -math.inf
        else peak + math.log(math.fsum(math.exp(value - peak) for value in values))
    )


def _probabilities(row, count):
    if not isinstance(row, (list, tuple)) or len(row) != count:
        raise ValueError("REGIME_PROBABILITY_SHAPE")
    values = tuple(_finite(x) for x in row)
    if any(x < 0 or x > 1 for x in values):
        raise ValueError("REGIME_PROBABILITY_SUM")
    total = math.fsum(values)
    if abs(total - 1) > 1e-9:
        raise ValueError("REGIME_PROBABILITY_SUM")
    return tuple(x / total for x in values)


def _fingerprint_payload(value):
    # Financial evidence forbids binary floats. Preserve exact model floats as
    # hexadecimal strings rather than relaxing the shared canonical serializer.
    if isinstance(value, dict):
        return {key: _fingerprint_payload(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_fingerprint_payload(item) for item in value]
    if type(value) is float:
        return value.hex()
    return value


@dataclass(frozen=True)
class GaussianRegimeModel:
    model_id: str
    training_end_ms: int
    available_at_ms: int
    training_digest: str
    scaler_training_digest: str
    scaler_fit_end_ms: int
    timeframe_minutes: int
    lookback: int
    start: tuple
    transition: tuple
    means: tuple
    variances: tuple
    scaler_mean: tuple
    scaler_scale: tuple

    def __post_init__(self):
        if (
            not isinstance(self.model_id, str)
            or not 1 <= len(self.model_id) <= 120
            or type(self.training_end_ms) is not int
            or type(self.available_at_ms) is not int
            or not 0 <= self.training_end_ms <= self.available_at_ms <= MAX_TIMESTAMP_MS
            or not isinstance(self.training_digest, str)
            or len(self.training_digest) != 64
            or any(c not in "0123456789abcdef" for c in self.training_digest)
            or not isinstance(self.scaler_training_digest, str)
            or self.scaler_training_digest != self.training_digest
            or type(self.scaler_fit_end_ms) is not int
            or not 0 <= self.scaler_fit_end_ms <= self.training_end_ms
            or type(self.timeframe_minutes) is not int
            or self.timeframe_minutes not in TIMEFRAMES
            or type(self.lookback) is not int
            or not 2 <= self.lookback <= 200
            or not isinstance(self.start, (tuple, list))
            or not 2 <= len(self.start) <= 5
        ):
            raise ValueError("REGIME_MODEL_CONTRACT")
        count, dimensions = len(self.start), len(FEATURES)
        object.__setattr__(self, "start", _probabilities(self.start, count))
        for name in ("transition", "means", "variances"):
            rows = getattr(self, name)
            if not isinstance(rows, (list, tuple)) or len(rows) != count:
                raise ValueError("REGIME_MODEL_SHAPE")
            if name == "transition":
                parsed = tuple(_probabilities(row, count) for row in rows)
            else:
                if any(
                    not isinstance(row, (list, tuple)) or len(row) != dimensions
                    for row in rows
                ):
                    raise ValueError("REGIME_MODEL_SHAPE")
                parsed = tuple(tuple(_finite(x) for x in row) for row in rows)
                if name == "variances" and any(
                    x < 1e-12 for row in parsed for x in row
                ):
                    raise ValueError("REGIME_VARIANCE_REQUIRED")
            object.__setattr__(self, name, parsed)
        for name in ("scaler_mean", "scaler_scale"):
            row = getattr(self, name)
            if not isinstance(row, (list, tuple)) or len(row) != dimensions:
                raise ValueError("REGIME_SCALER_SHAPE")
            parsed = tuple(_finite(x) for x in row)
            if name == "scaler_scale" and any(x < 1e-12 for x in parsed):
                raise ValueError("REGIME_SCALER_SCALE")
            object.__setattr__(self, name, parsed)

    @property
    def fingerprint(self):
        return digest(
            canonical(
                _fingerprint_payload(
                    {
                        "version": VERSION,
                        "feature_version": FEATURE_VERSION,
                        "feature_names": FEATURES,
                        **asdict(self),
                    }
                )
            )
        )


def causal_features(bars, *, timeframe_minutes, as_of_ms, lookback=20):
    """Feature rows use closed bars only; no full-sample normalization.

    Arrival timestamps are unavailable in the OHLCV archive. Consequently these
    rows support research, not proof that the live trader knew them at that time.
    """
    from v2_core.quant_indicators import Bar

    if (
        type(timeframe_minutes) is not int
        or timeframe_minutes not in TIMEFRAMES
        or type(lookback) is not int
        or not 2 <= lookback <= 200
        or type(as_of_ms) is not int
        or not 0 <= as_of_ms <= MAX_TIMESTAMP_MS
        or not isinstance(bars, (list, tuple))
        or not 1 <= len(bars) <= 10000
    ):
        raise ValueError("REGIME_FEATURE_BOUNDS")
    width = timeframe_minutes * 60000
    if any(not isinstance(b, Bar) for b in bars):
        raise ValueError("REGIME_FEATURE_GAP")
    closed = [bar for bar in bars if bar.t + width <= as_of_ms]
    # Later missing/misaligned bars cannot invalidate an earlier as-of result.
    # Within the visible prefix, never sort, fill gaps or accept duplicate bars.
    if any(b.t % width for b in closed) or any(
        b.t != closed[i - 1].t + width for i, b in enumerate(closed) if i
    ):
        raise ValueError("REGIME_FEATURE_GAP")
    output = []
    for i in range(lookback, len(closed)):
        window = closed[i - lookback : i + 1]
        closes, volumes = [float(b.c) for b in window], [float(b.v) for b in window]
        returns = [math.log(closes[j] / closes[j - 1]) for j in range(1, len(closes))]
        average = math.fsum(returns) / lookback
        volatility = math.sqrt(
            math.fsum((r - average) ** 2 for r in returns) / lookback
        )
        volume_base = math.fsum(volumes[:-1]) / lookback
        if volume_base <= 0:
            # Do not turn missing activity into a fabricated neutral feature.
            raise ValueError("REGIME_VOLUME_BASE_MISSING")
        current = window[-1]
        # Do not inherit a caller's precision, rounding or arithmetic traps.
        with localcontext(
            Context(
                prec=80,
                rounding=ROUND_HALF_EVEN,
                Emin=-999999,
                Emax=999999,
                capitals=1,
                clamp=0,
                flags=[],
                traps=[],
            )
        ):
            range_fraction = float((current.h - current.l) / current.c)
        values = (
            returns[-1],
            volatility,
            range_fraction,
            volumes[-1] / volume_base,
            closes[-1] / (math.fsum(closes[:-1]) / lookback) - 1,
        )
        if not all(math.isfinite(v) for v in values):
            raise ValueError("REGIME_FEATURE_NONFINITE")
        output.append(
            {
                "closed_at_ms": current.t + width,
                "values": list(values),
                "feature_version": FEATURE_VERSION,
                "timeframe_minutes": timeframe_minutes,
                "lookback": lookback,
            }
        )
    return output


def forward_filter(model, observations, *, timeframe_minutes, as_of_ms, max_age_ms):
    """Return p(state_t | observations_<=t), never a trading win probability.

    Model/scaler parameters must have been available BEFORE the first evaluated
    bar. Fixed parameters are immutable throughout this call; no fit/predict
    helper can silently consume future observations.
    """
    if (
        not isinstance(model, GaussianRegimeModel)
        or not isinstance(observations, (list, tuple))
        or not 1 <= len(observations) <= 10000
        or type(timeframe_minutes) is not int
        or timeframe_minutes not in TIMEFRAMES
        or timeframe_minutes != model.timeframe_minutes
        or type(as_of_ms) is not int
        or not 0 <= as_of_ms <= MAX_TIMESTAMP_MS
        or type(max_age_ms) is not int
        or not 1 <= max_age_ms <= 86400000
    ):
        raise ValueError("REGIME_FILTER_BOUNDS")
    width, count = timeframe_minutes * 60000, len(model.start)
    previous_time, log_posterior, rows = None, None, []
    log_transition = [
        [math.log(p) if p > 0 else -math.inf for p in row] for row in model.transition
    ]
    for observation in observations:
        if not isinstance(observation, dict) or set(observation) != {
            "closed_at_ms",
            "values",
            "feature_version",
            "timeframe_minutes",
            "lookback",
        }:
            raise ValueError("REGIME_OBSERVATION_SHAPE")
        if (
            observation["feature_version"] != FEATURE_VERSION
            or type(observation["timeframe_minutes"]) is not int
            or observation["timeframe_minutes"] != model.timeframe_minutes
            or type(observation["lookback"]) is not int
            or observation["lookback"] != model.lookback
        ):
            raise ValueError("REGIME_OBSERVATION_FEATURE_CONTRACT")
        stamp, raw = observation["closed_at_ms"], observation["values"]
        if (
            type(stamp) is not int
            or not 0 <= stamp <= MAX_TIMESTAMP_MS
            or stamp % width
            or stamp - width < model.available_at_ms
            or stamp > as_of_ms
            or (previous_time is not None and stamp != previous_time + width)
            or not isinstance(raw, (list, tuple))
            or len(raw) != len(FEATURES)
        ):
            raise ValueError("REGIME_OBSERVATION_TIME_OR_SHAPE")
        x = [
            (_finite(v) - model.scaler_mean[i]) / model.scaler_scale[i]
            for i, v in enumerate(raw)
        ]
        if not all(math.isfinite(value) for value in x):
            raise ValueError("REGIME_NUMERICAL_FAILURE")
        log_prior = (
            [math.log(p) if p > 0 else -math.inf for p in model.start]
            if log_posterior is None
            else [
                _logsumexp(
                    [log_posterior[j] + log_transition[j][k] for j in range(count)]
                )
                for k in range(count)
            ]
        )
        emissions = []
        for k in range(count):
            try:
                emission = -0.5 * math.fsum(
                    math.log(2 * math.pi)
                    + math.log(variance)
                    + ((value - mean) / math.sqrt(variance)) ** 2
                    for value, mean, variance in zip(
                        x, model.means[k], model.variances[k], strict=True
                    )
                )
            except (OverflowError, ValueError):
                raise ValueError("REGIME_NUMERICAL_FAILURE") from None
            if not math.isfinite(emission):
                raise ValueError("REGIME_NUMERICAL_FAILURE")
            emissions.append(emission)
        # Center emissions before adding priors, otherwise a finite but huge
        # shared negative density can round away all prior information.
        emission_peak = max(
            emission
            for emission, prior in zip(emissions, log_prior, strict=True)
            if prior != -math.inf
        )
        log_weight = [
            prior + (emission - emission_peak)
            for prior, emission in zip(log_prior, emissions, strict=True)
        ]
        peak = max(log_weight)
        if not math.isfinite(peak):
            raise ValueError("REGIME_NUMERICAL_FAILURE")
        shifted = [value - peak for value in log_weight]
        log_denominator = _logsumexp(shifted)
        log_posterior = [value - log_denominator for value in shifted]
        log_normalizer = emission_peak + peak + log_denominator
        posterior = [math.exp(value) for value in log_posterior]
        predictive = [
            math.exp(
                _logsumexp(
                    [log_posterior[j] + log_transition[j][k] for j in range(count)]
                )
            )
            for k in range(count)
        ]
        leader = max(range(count), key=lambda k: posterior[k])
        ranked = sorted(posterior, reverse=True)
        persistence = model.transition[leader][leader]
        rows.append(
            {
                "closed_at_ms": stamp,
                "filtered_probabilities": posterior,
                "next_state_probabilities": predictive,
                "dominant_state": leader,
                "probability_gap": ranked[0] - ranked[1],
                "expected_duration_bars": None
                if persistence == 1
                else 1 / (1 - persistence),
                "duration_status": "INFINITE_ABSORBING"
                if persistence == 1
                else "FINITE_GEOMETRIC",
                "scaled_log_predictive_density": log_normalizer,
            }
        )
        previous_time = stamp
    stale = as_of_ms - previous_time >= max_age_ms
    return {
        "version": VERSION,
        "model_id": model.model_id,
        "model_digest": model.fingerprint,
        "training_end_ms": model.training_end_ms,
        "available_at_ms": model.available_at_ms,
        "training_digest": model.training_digest,
        "scaler_training_digest": model.scaler_training_digest,
        "scaler_fit_end_ms": model.scaler_fit_end_ms,
        "feature_version": FEATURE_VERSION,
        "feature_names": list(FEATURES),
        "timeframe_minutes": model.timeframe_minutes,
        "lookback": model.lookback,
        "status": "STALE" if stale else "RESEARCH_ONLY",
        "observations": rows,
        "execution_authorized": False,
        "limitations": [
            "Latent state posterior is not a trading win probability.",
            "State numbers are not stable economic labels across refits.",
            "No fitted model, held-out profitability, calibrated event probability or live arrival proof is supplied.",
            "Expected duration is a geometric state run length including the current bar under a stationary transition matrix, not a holding-time instruction.",
            "Scaled log predictive density is a continuous density in standardized feature coordinates, not an event probability; cross-scaler comparisons require a Jacobian correction.",
            "Training/scaler digests and cutoff times are required declarations, not independently verified training artifacts.",
        ],
    }


def binary_calibration(probabilities, outcomes, *, bins=10):
    """Score an explicitly observable binary event, NOT an unobserved HMM label."""
    if (
        type(bins) is not int
        or not 2 <= bins <= 20
        or not isinstance(probabilities, (list, tuple))
        or not isinstance(outcomes, (list, tuple))
        or not 1 <= len(probabilities) == len(outcomes) <= 100000
    ):
        raise ValueError("REGIME_CALIBRATION_BOUNDS")
    probabilities = [_finite(p) for p in probabilities]
    if any(not 0 <= p <= 1 for p in probabilities) or any(
        type(y) is not int or y not in (0, 1) for y in outcomes
    ):
        raise ValueError("REGIME_CALIBRATION_EVENT_REQUIRED")
    groups = [[] for _ in range(bins)]
    for p, y in zip(probabilities, outcomes, strict=True):
        groups[min(bins - 1, int(p * bins))].append((p, y))
    return {
        "sample_count": len(outcomes),
        "brier_score": math.fsum(
            (p - y) ** 2 for p, y in zip(probabilities, outcomes, strict=True)
        )
        / len(outcomes),
        "reliability": [
            {
                "bin": i,
                "count": len(group),
                "mean_probability": math.fsum(p for p, _ in group) / len(group)
                if group
                else None,
                "event_frequency": math.fsum(y for _, y in group) / len(group)
                if group
                else None,
            }
            for i, group in enumerate(groups)
        ],
        "execution_authorized": False,
    }
