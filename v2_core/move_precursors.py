"""Causal, bounded OHLCV descriptors for mover research; no I/O or execution.

The caller fixes the cutoff before examining outcomes. Equal-length baseline and
recent windows use only candles whose close boundary is at or before that cutoff.
Observing a pattern is not evidence of predictability or an identifiable trader's
inventory. Spot flow, continuous depth and derivative positioning remain unknown.
"""

from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext

from v2_core.evidence import canonical, digest

VERSION = "move-precursors-ohlcv-v1"
MINUTE = 60_000
_CONTEXT = Context(prec=60, rounding=ROUND_HALF_EVEN)
_SOURCES = {"BINANCE_USDM_LIVE", "BINANCE_USDM_SANDBOX"}
_LIMITATIONS = (
    "Cutoff and side must be fixed independently of future mover rankings.",
    "Closed-candle event time does not prove historical receipt or decision time.",
    "OHLCV cannot identify spot buying, participant inventory, hedges or intent.",
    "One depth snapshot cannot establish absorption, replenishment or cancellations.",
    "Prediction requires failures, contemporaneous controls and held-out periods.",
    "Descriptors are not an entry decision, probability or achievable return.",
)


def _timestamp(value):
    return type(value) is int and 0 <= value < 2**53


def _number(value, *, positive):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("INVALID_DECIMAL")  # noqa: TRY004 - stable evidence reason
    if len(str(value)) > 100:
        raise ValueError("DECIMAL_BOUND")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("INVALID_DECIMAL") from exc
    if not number.is_finite() or number < 0 or (positive and not number):
        raise ValueError("INVALID_DECIMAL")
    if number and not -40 <= number.adjusted() <= 40:
        raise ValueError("DECIMAL_BOUND")
    return number


def _text(value):
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _hypotheses(observed):
    # These are testable research questions, not classifications of intent.
    return [
        {
            "id": "PRICE_VOLUME_STRUCTURE",
            "status": "DESCRIPTIVE_ONLY" if observed else "UNKNOWN",
            "hypothesis": (
                "Pre-cutoff range contraction, relative volume and directional "
                "persistence add information about later directional continuation."
            ),
            "required_evidence": [
                "CONTIGUOUS_CLOSED_OHLCV",
                "PREDECLARED_CUTOFF_AND_OUTCOME_HORIZON",
                "FAILED_PATTERNS_AND_MATCHED_NON_MOVER_CONTROLS",
                "TIME_ORDERED_HELD_OUT_EVALUATION",
            ],
            "falsification": (
                "Reject predictive value if held-out continuation and lead time "
                "do not improve over matched controls after executable costs."
            ),
        },
        {
            "id": "SPOT_DIRECTIONAL_FLOW",
            "status": "UNKNOWN",
            "reason": "SPOT_EXECUTED_FLOW_NOT_SUPPLIED",
            "hypothesis": (
                "Persistent same-direction spot aggressor flow precedes some "
                "futures continuations."
            ),
            "required_evidence": [
                "MAPPED_SPOT_INSTRUMENT_AND_UNITS",
                "SPOT_EXECUTED_BUY_SELL_NOTIONAL_IN_PRE_CUTOFF_WINDOWS",
                "SOURCE_EVENT_AND_RECEIPT_TIMES_WITH_GAP_COVERAGE",
                "CONTEMPORANEOUS_FUTURES_PRICE_AND_CONTROLS",
            ],
            "falsification": (
                "Reject this mechanism for a case when flow starts after the "
                "move or is absent; reject prediction if controls show the same "
                "pattern. Passive accumulation can have opposite aggressor flow."
            ),
        },
        {
            "id": "LIQUIDITY_ABSORPTION",
            "status": "UNKNOWN",
            "reason": "CONTINUOUS_BOOK_AND_EXECUTED_TRADES_NOT_SUPPLIED",
            "hypothesis": (
                "Repeated executed flow with limited adverse price response "
                "and observable replenishment precedes directional continuation."
            ),
            "required_evidence": [
                "SEQUENCE_COMPLETE_BOOK_UPDATES_AND_EXECUTED_TRADES",
                "SOURCE_EVENT_AND_RECEIPT_TIMES",
                "PRICE_RESPONSE_AND_REPLENISHMENT_BASELINE",
                "FAILED_ABSORPTION_PATTERNS",
            ],
            "falsification": (
                "Reject the interpretation if displayed depth disappears without "
                "matching executions, the sequence is incomplete, or price "
                "response and continuation do not differ from controls."
            ),
        },
        {
            "id": "DERIVATIVES_POSITIONING",
            "status": "UNKNOWN",
            "reason": "TIME_ALIGNED_OI_FUNDING_AND_LIQUIDATIONS_NOT_SUPPLIED",
            "hypothesis": (
                "Pre-cutoff positioning and funding distinguish continuation "
                "from position-closing bursts better than price alone."
            ),
            "required_evidence": [
                "PRE_CUTOFF_BASE_UNIT_OPEN_INTEREST_AND_FUNDING",
                "PRICE_AND_SPOT_FLOW_IN_MATCHED_WINDOWS",
                "LIQUIDATION_STREAM_COVERAGE_AND_TIMESTAMPS",
                "HELD_OUT_PRICE_ONLY_BASELINE",
            ],
            "falsification": (
                "Reject predictive value if added observations have no held-out "
                "gain; OI changes and funding alone do not identify trader "
                "direction, new risk, forced trades or manipulation."
            ),
        },
    ]


def _select(rows, *, start, cutoff, width, symbol, market_source):
    if not isinstance(rows, (list, tuple)) or len(rows) > 10_000:
        raise ValueError("SOURCE_ROWS_BOUND")
    selected = {}
    for row in rows:
        if not isinstance(row, dict) or not _timestamp(row.get("t")):
            raise ValueError("INVALID_BAR_TIMESTAMP")
        opened = row["t"]
        if not start <= opened < cutoff:
            # No future or older price, metadata or malformed OHLC can alter
            # these features or their content address.
            continue
        if opened % width:
            raise ValueError("MISALIGNED_BAR")
        if opened in selected:
            raise ValueError("DUPLICATE_BAR")
        if row.get("symbol", symbol) != symbol:
            raise ValueError("BAR_SYMBOL_MISMATCH")
        if row.get("market_source", market_source) != market_source:
            raise ValueError("BAR_SOURCE_MISMATCH")
        expected_environment = market_source.removeprefix("BINANCE_USDM_")
        if row.get("environment", expected_environment) != expected_environment:
            raise ValueError("BAR_SOURCE_MISMATCH")
        values = {
            key: _number(row[key], positive=key != "v")
            for key in ("o", "h", "l", "c", "v")
        }
        if not (
            values["l"]
            <= min(values["o"], values["c"])
            <= max(values["o"], values["c"])
            <= values["h"]
        ):
            raise ValueError("INVALID_OHLC")
        selected[opened] = {"t": opened, **values}
    if len(selected) != (cutoff - start) // width:
        raise ValueError("MISSING_OR_PARTIAL_WINDOW")
    return [selected[t] for t in sorted(selected)]


def _describe(rows, *, direction):
    opening, closing = rows[0]["o"], rows[-1]["c"]
    spread = max(r["h"] for r in rows) - min(r["l"] for r in rows)
    previous, favorable = opening, 0
    for row in rows:
        favorable += direction * (row["c"] - previous) > 0
        previous = row["c"]
    return {
        "open": opening,
        "close": closing,
        "range_pct_of_open": spread / opening * 100,
        "base_volume": sum((r["v"] for r in rows), Decimal(0)),
        "directional_return_pct": direction * (closing / opening - 1) * 100,
        "favorable_close_step_pct": Decimal(favorable) / len(rows) * 100,
    }


def analyze_move_precursor(
    rows,
    *,
    symbol,
    side,
    cutoff_ms,
    interval_minutes=1,
    window_bars=30,
    market_source="BINANCE_USDM_LIVE",
):
    """Describe two closed, equal windows, never choose a favorable cutoff.

    Each row is ``{t, o, h, l, c, v}``, where t is candle open milliseconds.
    ``v`` is base-asset volume. Optional per-row symbol/market_source/environment
    assertions must agree with the caller's source. The caller attests provenance
    and closure: this pure function has no clock or historical receipt archive.

    Fractions are encoded as decimal strings; zero denominators stay unknown.
    Unusable source data returns UNKNOWN, invalid configuration raises ValueError.
    Both direction snapshots can be evaluated at a predeclared cutoff. A side
    chosen from the end-of-day ranking must be labelled retrospective by callers.
    """
    if (
        not isinstance(symbol, str)
        or not 5 <= len(symbol) <= 44
        or not symbol.endswith("USDT")
        or any(not (c.isalnum() or c == "_") for c in symbol[:-4])
        or side not in {"BUY", "SELL"}
        or not isinstance(market_source, str)
        or market_source not in _SOURCES
        or type(interval_minutes) is not int
        or interval_minutes not in {1, 5, 15, 30, 60}
        or type(window_bars) is not int
        or not 2 <= window_bars <= 240
        or 2 * window_bars * interval_minutes > 10_080
        or not _timestamp(cutoff_ms)
    ):
        raise ValueError("PRECURSOR_CONFIGURATION_INVALID")
    width = interval_minutes * MINUTE
    start, split = cutoff_ms - 2 * window_bars * width, cutoff_ms - window_bars * width
    if start < 0 or cutoff_ms % width:
        raise ValueError("PRECURSOR_CUTOFF_UNALIGNED_OR_INSUFFICIENT")
    report = {
        "version": VERSION,
        "status": "UNKNOWN",
        "reason": None,
        "symbol": symbol,
        "side": side,
        "market_source": market_source,
        "cutoff_ms": cutoff_ms,
        "source_interval_minutes": interval_minutes,
        "window_bars": window_bars,
        "windows": {
            "baseline": {"start_ms": start, "end_ms": split},
            "recent": {"start_ms": split, "end_ms": cutoff_ms},
        },
        "features": None,
        "input_digest": None,
        "hypotheses": _hypotheses(False),
        "limitations": list(_LIMITATIONS),
    }
    with localcontext(_CONTEXT):
        try:
            selected = _select(
                rows,
                start=start,
                cutoff=cutoff_ms,
                width=width,
                symbol=symbol,
                market_source=market_source,
            )
        except (ValueError, KeyError) as exc:
            report["reason"] = (
                str(exc) if isinstance(exc, ValueError) else "MISSING_BAR_FIELD"
            )
            return report
        direction = Decimal(1 if side == "BUY" else -1)
        baseline = _describe(selected[:window_bars], direction=direction)
        recent = _describe(selected[window_bars:], direction=direction)
        missing = []
        ratios = {}
        for output, key in (
            ("range_ratio", "range_pct_of_open"),
            ("volume_ratio", "base_volume"),
        ):
            ratios[output] = None
            if baseline[key]:
                ratios[output] = _text(recent[key] / baseline[key])
            else:
                missing.append("ZERO_BASELINE_" + key.upper())
        normalized = [
            {"t": r["t"], **{k: _text(r[k]) for k in ("o", "h", "l", "c", "v")}}
            for r in selected
        ]
        report.update(
            status="OBSERVED",
            features={
                **ratios,
                "directional_return_pct": _text(recent["directional_return_pct"]),
                "favorable_close_step_pct": _text(recent["favorable_close_step_pct"]),
                "undefined_ratios": missing,
                "volume_unit": "BASE_ASSET",
            },
            input_digest=digest(
                canonical(
                    {
                        "version": VERSION,
                        "symbol": symbol,
                        "side": side,
                        "market_source": market_source,
                        "cutoff_ms": cutoff_ms,
                        "interval_minutes": interval_minutes,
                        "window_bars": window_bars,
                        "bars": normalized,
                    }
                )
            ),
            hypotheses=_hypotheses(True),
        )
        for name, values in (("baseline", baseline), ("recent", recent)):
            report["windows"][name].update(
                {key: _text(value) for key, value in values.items()}
            )
        return report
