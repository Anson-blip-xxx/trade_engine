"""Bounded observational paths for setup shadow studies; never an order port.

The supplied reader must be ReentryArchive.read (or the same dedicated, scoped
SANDBOX port) with each call bounded to three seconds by its client. The actual
archive returns bare OHLCV dictionaries in descending time order: source,
environment and symbol are established by that port's scoped query, not invented
as independently authenticated fields on a candle. Optional row provenance is
checked when present. No network clients, files, database writes or trading
chain hooks are created here.
"""

import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from services.v2_trade_path_review import post_flat_path
from v2_core.quant_indicators import MINUTE, Bar
from v2_core.symbols import is_supported_symbol

VERSION = "setup-shadow-observed-paths-v1"
SOURCE = "BINANCE_USDM_SANDBOX"
HORIZONS = (30, 60, 240)
MAX_CASES = 12
MAX_SECONDS = 15
MAX_OUTPUT_BYTES = 512 * 1024
_MAX_INPUT_BYTES = 8 * 1024 * 1024
_ALTERNATIVES = ("BREAKOUT_VOLUME075", "PULLBACK_VOLUME075")
_FLAGS = ("execution_authorized", "automatic_parameter_updates", "promotion_authorized")


class _Invalid(ValueError):
    """Only locally constructed constant codes cross the research boundary."""


def _encode(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_encode(value)).hexdigest()


def _stamp(value):
    return type(value) is int and 0 <= value < 2**53


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _empty(as_of_ms):
    return {
        "version": VERSION,
        "status": "UNKNOWN",
        "reason": None,
        "research_only": True,
        "source": SOURCE,
        "environment": "SANDBOX",
        "as_of_ms": as_of_ms if _stamp(as_of_ms) else None,
        **dict.fromkeys(_FLAGS, False),
        "scope_key": None,
        "account_environment": "SANDBOX",
        "window_start_ms": None,
        "window_end_ms": None,
        "day": None,
        "plan_digest": None,
        "input_digest": None,
        "study_content_digest": None,
        "cases": [],
        "input_sample_count": 0,
        "selected_valid_count": 0,
        "selected_count": 0,
        "selection_truncated": False,
        "selection_basis": "FIXED_EVENLY_SPACED_DECISION_REF_METADATA_MAX_12",
        "status_counts": {},
        "validation_reasons": {},
        "coverage": {
            "query_complete": False,
            "retryable": False,
            "all_windows_observed": False,
        },
        "budget": {"calls": 0, "max_calls": 12, "max_seconds": 15},
        "limitations": [
            "FIXED_METADATA_SELECTION_PRECEDES_VALIDATION_AND_CANDIDATE_LABELS",
            "INVALID_SELECTED_SAMPLES_REMAIN_UNKNOWN_WITHOUT_REPLACEMENT",
            "DETERMINISTIC_SAMPLES_NOT_RANDOM_OR_PROVEN_INDEPENDENT",
            "SAME_SANDBOX_ARCHIVE_QUERY_PROVENANCE_NOT_INDEPENDENT_AUTHENTICATION",
            "FIRST_FULLY_AFTER_DECISION_MINUTE_CLOSE_IS_AN_OBSERVATION_NOT_FILL_PRICE",
            "HORIZONS_START_AT_OBSERVATION_ANCHOR_CLOSE",
            "DIRECTIONAL_MARKET_CHANGES_NOT_NET_PNL_OR_BACKTEST_WIN_RATE",
            "NO_EXECUTION_FEES_SLIPPAGE_OR_INTRABAR_EXECUTION_ORDER_ASSUMED",
            "POST_DECISION_OBSERVATIONS_WERE_NOT_AVAILABLE_AT_DECISION",
        ],
    }


def _seal(result):
    result["content_digest"] = _digest(result)
    if len(_encode(result)) > MAX_OUTPUT_BYTES:
        result = _empty(result.get("as_of_ms"))
        result["reason"] = "PATH_OUTPUT_BOUND"
        result["content_digest"] = _digest(result)
    return result


def _snapshot(study, as_of_ms):
    if not _stamp(as_of_ms) or not isinstance(study, dict):
        raise _Invalid("PATH_STUDY_INVALID")
    encoded = _encode(study)
    if len(encoded) > _MAX_INPUT_BYTES:
        raise _Invalid("PATH_INPUT_BOUND")
    frozen = json.loads(encoded)
    fingerprint = frozen.pop("content_digest", None)
    if not _sha(fingerprint) or _digest(frozen) != fingerprint:
        raise _Invalid("PATH_STUDY_DIGEST_MISMATCH")
    frozen["content_digest"] = fingerprint
    if (
        frozen.get("version") != "setup-shadow-study-v1"
        or not _sha(frozen.get("scope_key"))
        or frozen.get("account_environment") not in ("SANDBOX", "LIVE")
    ):
        raise _Invalid("PATH_SANDBOX_SCOPE_REQUIRED")
    if any(frozen.get(flag) is not False for flag in _FLAGS):
        raise _Invalid("PATH_RESEARCH_PERMISSIONS_INVALID")
    if (
        frozen.get("as_of_ms") != as_of_ms
        or not _sha(frozen.get("plan_digest"))
        or not _sha(frozen.get("input_digest"))
    ):
        raise _Invalid("PATH_STUDY_BINDING_INVALID")
    start, end = frozen.get("window_start_ms"), frozen.get("window_end_ms")
    if (
        not _stamp(start)
        or not _stamp(end)
        or end - start != 1440 * MINUTE
        or (start + 480 * MINUTE) % (1440 * MINUTE)
        or end > as_of_ms
    ):
        raise _Invalid("PATH_DAY_INVALID")
    samples = frozen.get("samples")
    if not isinstance(samples, list) or len(samples) > 96:
        raise _Invalid("PATH_SAMPLE_BOUND")
    return frozen, start, end


def _candidate_plans(sample):
    plans = ["BASELINE"] if sample.get("baseline_market_reason") == "CANDIDATE" else []
    alternatives = sample.get("alternatives")
    if not isinstance(alternatives, dict):
        raise _Invalid("PATH_SAMPLE_ALTERNATIVES_INVALID")
    for name in _ALTERNATIVES:
        value = alternatives.get(name)
        if not isinstance(value, dict):
            raise _Invalid("PATH_SAMPLE_ALTERNATIVES_INVALID")
        if value.get("market_reason") == "CANDIDATE":
            plans.append(name)
    return plans


def _selected(study):
    # Invalid entries also occupy their predetermined slot. Neither observed
    # performance, candidate status nor validation success can trigger backfill.
    samples = [
        sample if isinstance(sample, dict) else {} for sample in study["samples"]
    ]
    refs = Counter(
        sample.get("opportunity_ref")
        for sample in samples
        if _sha(sample.get("opportunity_ref"))
    )
    samples.sort(
        key=lambda sample: (
            sample["decided_ms"] if _stamp(sample.get("decided_ms")) else 2**53,
            sample["opportunity_ref"] if _sha(sample.get("opportunity_ref")) else "~",
        )
    )
    count = len(samples)
    indices = (
        range(count)
        if count <= MAX_CASES
        else [((2 * i + 1) * count) // (2 * MAX_CASES) for i in range(MAX_CASES)]
    )
    return [(index, samples[index]) for index in indices], refs


def _validate_sample(sample, refs, *, start, end, as_of_ms):
    if not _sha(sample.get("opportunity_ref")):
        raise _Invalid("PATH_OPPORTUNITY_ID_INVALID")
    if refs[sample["opportunity_ref"]] != 1:
        raise _Invalid("PATH_DUPLICATE_OPPORTUNITY")
    if not is_supported_symbol(
        sample.get("symbol"), quote_assets=("USDT",)
    ) or sample.get("side") not in ("BUY", "SELL"):
        raise _Invalid("PATH_SAMPLE_SYMBOL_OR_SIDE_INVALID")
    if sample.get("environment") != "SANDBOX":
        raise _Invalid("PATH_SAMPLE_ENVIRONMENT_MISMATCH")
    if sample.get("status") != "BASELINE_MATCHED":
        raise _Invalid("PATH_BASELINE_UNPROVEN")
    observed, decided, bar_close = (
        sample.get(key) for key in ("observed_ms", "decided_ms", "bar_close_ms")
    )
    if (
        any(not _stamp(t) for t in (observed, decided, bar_close))
        or not start <= decided < end
        or not bar_close <= observed <= decided <= as_of_ms
        or bar_close % MINUTE
    ):
        raise _Invalid("PATH_SAMPLE_TIMING_INVALID")
    return _candidate_plans(sample)


def _bars(raw, *, target, start, end, as_of_ms):
    if not isinstance(raw, list) or len(raw) > (end - start) // MINUTE:
        raise _Invalid("PATH_ARCHIVE_PAGE_BOUND")
    bars, seen = [], set()
    for row in raw:
        if not isinstance(row, dict):
            raise _Invalid("PATH_ARCHIVE_BAR_INVALID")
        if any(
            key in row and row[key] != value
            for key, value in (
                ("source", SOURCE),
                ("environment", "SANDBOX"),
                ("symbol", target),
            )
        ):
            raise _Invalid("PATH_ARCHIVE_PROVENANCE_MISMATCH")
        try:
            bar = Bar(**{key: row[key] for key in ("t", "o", "h", "l", "c", "v")})
        except (KeyError, ValueError, TypeError, ArithmeticError):
            raise _Invalid("PATH_ARCHIVE_BAR_INVALID") from None
        if bar.t % MINUTE or not start <= bar.t < end or bar.t + MINUTE > as_of_ms:
            raise _Invalid("PATH_ARCHIVE_TIME_MISMATCH")
        if bar.t in seen:
            raise _Invalid("PATH_ARCHIVE_DUPLICATE_MINUTE")
        seen.add(bar.t)
        bars.append(bar)
    return sorted(bars, key=lambda bar: bar.t)


def _window(horizon, anchor_ms):
    return {
        "status": "PENDING",
        "reason": "PATH_HORIZON_NOT_MATURE",
        "horizon_minutes": horizon,
        "anchor_ms": anchor_ms,
        "observed_end_ms": anchor_ms + horizon * MINUTE
        if anchor_ms is not None
        else None,
        "return_pct": None,
        "favorable_excursion_pct": None,
        "adverse_excursion_pct": None,
        "market_return_not_trade_pnl": True,
        "costs_included": False,
        "counterfactual_execution_proven": False,
    }


def _case(sample, plans):
    decided = sample.get("decided_ms")
    first = (decided + MINUTE - 1) // MINUTE * MINUTE if _stamp(decided) else None
    anchor_ms = first + MINUTE if first is not None else None
    return {
        **{
            key: sample.get(key) if _stamp(sample.get(key)) else None
            for key in ("observed_ms", "decided_ms", "bar_close_ms")
        },
        "opportunity_ref": sample.get("opportunity_ref")
        if _sha(sample.get("opportunity_ref"))
        else None,
        "symbol": sample.get("symbol")
        if is_supported_symbol(sample.get("symbol"), quote_assets=("USDT",))
        else None,
        "side": sample.get("side") if sample.get("side") in ("BUY", "SELL") else None,
        "source": SOURCE,
        "environment": "SANDBOX",
        "status": "PENDING",
        "candidate_plans": plans,
        "candidate_classification": "ANY_PLAN_CANDIDATE"
        if plans
        else "NO_PLAN_CANDIDATE_CONTROL",
        "anchor_basis": "FIRST_FULLY_AFTER_DECISION_MINUTE_CLOSE",
        "anchor_is_execution_price": False,
        "anchor_ms": anchor_ms,
        "anchor_delay_ms": anchor_ms - decided if anchor_ms is not None else None,
        "anchor_price": None,
        "windows": {str(horizon): _window(horizon, anchor_ms) for horizon in HORIZONS},
    }, first


def _observe(case, bars, *, first, as_of_ms, binding):
    by_time = {bar.t: bar for bar in bars}
    anchor = by_time.get(first)
    if anchor is not None:
        case["anchor_price"] = str(anchor.c)
    for horizon in HORIZONS:
        window = case["windows"][str(horizon)]
        target_end = window["observed_end_ms"]
        if target_end > as_of_ms:
            continue
        expected = list(range(first, target_end, MINUTE))
        if anchor is None or any(t not in by_time for t in expected):
            window.update(
                status="UNKNOWN", reason="PATH_CONTIGUOUS_MINUTES_UNAVAILABLE"
            )
            continue
        path = [by_time[t] for t in expected[1:]]
        observed = post_flat_path(
            path,
            side=case["side"],
            closed_ms=case["anchor_ms"],
            as_of_ms=as_of_ms,
            horizon_minutes=horizon,
            anchor={
                "kind": "MARKET_MARK",
                "t": case["anchor_ms"],
                "price": str(anchor.c),
                "observed_ms": case["anchor_ms"],
                "source": SOURCE,
            },
        )
        if observed["status"] != "OBSERVED":
            window.update(status="UNKNOWN", reason="PATH_VALIDATION_UNAVAILABLE")
            continue
        window.update(status="OBSERVED", reason=None)
        for key in (
            "return_pct",
            "favorable_excursion_pct",
            "adverse_excursion_pct",
            "end_price",
        ):
            window[key] = observed[key]
        window["path_input_digest"] = _digest(
            {
                **binding,
                "opportunity_ref": case["opportunity_ref"],
                "symbol": case["symbol"],
                "side": case["side"],
                "decided_ms": case["decided_ms"],
                "horizon_minutes": horizon,
                "source": SOURCE,
                "bars": [by_time[t].payload() for t in expected],
            }
        )


def enrich_setup_paths(study, reader, *, as_of_ms):
    """Return digest-bound observations; all input/reader failures are isolated.

    At most 12 predetermined samples and 12 reads are attempted. A read starts
    only with three seconds remaining in the 15 second budget. Identical symbol and
    range requests share an immutable validated cache within this invocation only.
    """
    result = _empty(as_of_ms)
    try:
        frozen, start, end = _snapshot(study, as_of_ms)
        for key in (
            "scope_key",
            "account_environment",
            "window_start_ms",
            "window_end_ms",
            "plan_digest",
            "input_digest",
        ):
            result[key] = frozen[key]
        result["day"] = (
            datetime.fromtimestamp(start / 1000, ZoneInfo("Asia/Shanghai"))
            .date()
            .isoformat()
        )
        result["study_content_digest"] = frozen["content_digest"]
        result["environment"] = frozen["account_environment"]
        if frozen["account_environment"] != "SANDBOX":
            raise _Invalid("PATH_SANDBOX_SCOPE_REQUIRED")
        if not callable(reader):
            raise _Invalid("PATH_READER_INVALID")
        binding = {
            key: result[key]
            for key in (
                "scope_key",
                "account_environment",
                "window_start_ms",
                "window_end_ms",
                "environment",
                "day",
                "as_of_ms",
                "plan_digest",
                "input_digest",
                "study_content_digest",
            )
        }
        selected, refs = _selected(frozen)
        result.update(
            input_sample_count=len(frozen["samples"]),
            selected_count=len(selected),
            selection_truncated=len(frozen["samples"]) > len(selected),
        )
        deadline, cache = time.monotonic() + MAX_SECONDS, {}
        validation_reasons = Counter()
        for selection_index, sample in selected:
            reason, plans = None, []
            try:
                plans = _validate_sample(
                    sample, refs, start=start, end=end, as_of_ms=as_of_ms
                )
            except _Invalid as exc:
                reason = str(exc)
            case, first = _case(sample, plans)
            case["selection_index"] = selection_index
            if reason:
                case.update(
                    status="UNKNOWN", reason=reason, candidate_classification="UNKNOWN"
                )
                for window in case["windows"].values():
                    window.update(status="UNKNOWN", reason=reason)
                validation_reasons[reason] += 1
                result["cases"].append(case)
                continue
            result["selected_valid_count"] += 1
            mature = [h for h in HORIZONS if case["anchor_ms"] + h * MINUTE <= as_of_ms]
            if mature:
                stop = case["anchor_ms"] + max(mature) * MINUTE
                key = (case["symbol"], first, stop)
                try:
                    if key not in cache:
                        if (
                            time.monotonic() > deadline - 3
                            or result["budget"]["calls"] >= MAX_CASES
                        ):
                            raise _Invalid("PATH_ARCHIVE_BUDGET_EXHAUSTED")
                        result["budget"]["calls"] += 1
                        try:
                            raw = reader(case["symbol"], first, stop)
                        except Exception:  # noqa: BLE001 - isolate the bounded external reader
                            raise _Invalid("PATH_ARCHIVE_READ_UNAVAILABLE") from None
                        if time.monotonic() > deadline:
                            raise _Invalid("PATH_ARCHIVE_BUDGET_EXHAUSTED")
                        cache[key] = tuple(
                            _bars(
                                raw,
                                target=case["symbol"],
                                start=first,
                                end=stop,
                                as_of_ms=as_of_ms,
                            )
                        )
                    _observe(
                        case,
                        cache[key],
                        first=first,
                        as_of_ms=as_of_ms,
                        binding=binding,
                    )
                except _Invalid as exc:
                    for horizon in mature:
                        case["windows"][str(horizon)].update(
                            status="UNKNOWN", reason=str(exc)
                        )
                except Exception:  # noqa: BLE001 - optional path must not interrupt other cases
                    for horizon in mature:
                        case["windows"][str(horizon)].update(
                            status="UNKNOWN", reason="PATH_VALIDATION_UNAVAILABLE"
                        )
            states = {window["status"] for window in case["windows"].values()}
            case["status"] = (
                "OBSERVED"
                if "OBSERVED" in states
                else "UNKNOWN"
                if "UNKNOWN" in states
                else "PENDING"
            )
            result["cases"].append(case)
        result["status_counts"] = dict(
            sorted(Counter(case["status"] for case in result["cases"]).items())
        )
        result["validation_reasons"] = dict(sorted(validation_reasons.items()))
        windows = [
            window for case in result["cases"] for window in case["windows"].values()
        ]
        transient_read_reasons = {
            "PATH_ARCHIVE_READ_UNAVAILABLE",
            "PATH_ARCHIVE_BUDGET_EXHAUSTED",
        }
        reads_complete = not any(
            window["reason"] in transient_read_reasons for window in windows
        )
        # A completed query that returns a gap is complete as a query, but not
        # usable path evidence. Do not repeatedly retry permanent archive gaps.
        result["coverage"] = {
            "query_complete": reads_complete,
            "retryable": not reads_complete
            or any(window["status"] == "PENDING" for window in windows),
            "all_windows_observed": bool(windows)
            and all(window["status"] == "OBSERVED" for window in windows),
        }
        result["status"] = "AVAILABLE" if selected else "NO_SAMPLES"
    except _Invalid as exc:
        result["reason"] = str(exc)
    except Exception:  # noqa: BLE001 - untrusted evidence must not affect its caller
        result["reason"] = "PATH_STUDY_VALIDATION_UNAVAILABLE"
    return _seal(result)
