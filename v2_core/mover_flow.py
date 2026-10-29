"""Pure, bounded event-time flow descriptors for retrospective mover research.

The caller fixes the side and cutoff independently of later outcomes. Public
historical API rows do not establish what a strategy actually received then.
No trade decision, probability, participant identity or forced trade is inferred.
"""

from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext

from v2_core.evidence import canonical, digest

VERSION = "mover-flow-evidence-v1"
BUNDLE_VERSION = "mover-flow-public-v1"
HOUR = 3_600_000
DAY = 24 * HOUR
_CONTEXT = Context(prec=80, rounding=ROUND_HALF_EVEN)
_MAX_ROWS = 512
_LIMITATIONS = (
    "The cutoff and side must be fixed independently of future mover rankings.",
    "Aggressor buy minus sell notional is not net capital inflow or trader inventory.",
    "Open interest counts outstanding contracts in base units, not net long exposure.",
    "Actual funding is a past settlement rate; its interval and future rate are unknown.",
    "Historical event time and a declared fetch timestamp do not prove historical receipt.",
    "Current exact spot mapping does not prove that the instrument was tradable then.",
    "Price, flow, open interest and funding alone cannot identify absorption, liquidation or intent.",
    "These descriptors are retrospective research, not an order, recommendation or probability.",
)


def _timestamp(value):
    return type(value) is int and 0 <= value < 2**53


def _symbol(value):
    return (
        isinstance(value, str)
        and 5 <= len(value) <= 44
        and value.endswith("USDT")
        and all(c.isalnum() or c == "_" for c in value[:-4])
    )


def _number(value, *, positive=False, signed=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("INVALID_DECIMAL")  # noqa: TRY004 - stable evidence reason
    if isinstance(value, int) and abs(value) >= 10**41:
        raise ValueError("DECIMAL_BOUND")
    if len(str(value)) > 100:
        raise ValueError("DECIMAL_BOUND")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("INVALID_DECIMAL") from exc
    if not number.is_finite():
        raise ValueError("INVALID_DECIMAL")
    if (
        not -40 <= number.as_tuple().exponent <= 40
        or len(number.as_tuple().digits) > 80
    ):
        raise ValueError("DECIMAL_BOUND")
    if number and not -40 <= number.adjusted() <= 40:
        raise ValueError("DECIMAL_BOUND")
    if (not signed and number < 0) or (positive and number <= 0):
        raise ValueError("INVALID_DECIMAL")
    return number


def _text(value):
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _unknown(reason, *, receipt=None):
    return {
        "status": "UNKNOWN",
        "reason": reason,
        "features": None,
        "receipt": receipt,
        "input_digest": None,
    }


def _receipt(envelope, cutoff):
    fetched = envelope.get("fetched_at_ms")
    if not _timestamp(fetched):
        raise ValueError("INVALID_FETCH_TIMESTAMP")
    retrospective = fetched > cutoff
    return {
        "fetched_at_ms": fetched,
        "status": (
            "HISTORICAL_RECEIPT_UNPROVEN"
            if retrospective
            else "RECEIPT_REPORTED_AT_OR_BEFORE_CUTOFF"
        ),
        "availability": (
            "RETROSPECTIVE_RESEARCH_ONLY"
            if retrospective
            else "DECLARED_FETCH_TIME_ONLY"
        ),
        "historical_receipt_proven": False,
    }


def _envelope(envelope, *, source, cutoff):
    if not isinstance(envelope, dict):
        raise ValueError("SOURCE_NOT_SUPPLIED")  # noqa: TRY004 - stable evidence reason
    if envelope.get("status") != "OBSERVED":
        reason = envelope.get("reason")
        # Reader reason codes are useful; arbitrary upstream prose is not evidence.
        if (
            isinstance(reason, str)
            and 1 <= len(reason) <= 80
            and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in reason)
        ):
            raise ValueError(reason)
        raise ValueError("SOURCE_NOT_OBSERVED")
    if envelope.get("source") != source:
        raise ValueError("SOURCE_MISMATCH")
    receipt = _receipt(envelope, cutoff)
    rows = envelope.get("rows")
    if not isinstance(rows, (list, tuple)) or len(rows) > _MAX_ROWS:
        raise ValueError("SOURCE_ROWS_BOUND")
    return rows, receipt


def _validate_mapping(mapping, symbol):
    if not isinstance(mapping, dict) or mapping.get("status") != "MATCHED":
        raise ValueError("SPOT_MAPPING_UNKNOWN")
    if (
        mapping.get("basis") != "CURRENT_EXACT_SYMBOL_BASE_QUOTE"
        or mapping.get("spot_symbol") != symbol
        or mapping.get("base_asset") != symbol[:-4]
        or mapping.get("quote_asset") != "USDT"
    ):
        raise ValueError("SPOT_MAPPING_MISMATCH")


def _spot_rows(rows, *, start, cutoff):
    selected = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or not row or not _timestamp(row[0]):
            raise ValueError("INVALID_SPOT_TIMESTAMP")
        opened = row[0]
        if not start <= opened < cutoff:
            continue  # Unused future values, metadata and duplicates cannot leak.
        if opened % HOUR:
            raise ValueError("MISALIGNED_SPOT_BAR")
        if opened in selected:
            raise ValueError("DUPLICATE_SPOT_BAR")
        if len(row) != 12:
            raise ValueError("INVALID_SPOT_ROW")
        if not _timestamp(row[6]) or row[6] != opened + HOUR - 1:
            raise ValueError("INVALID_SPOT_CLOSE_TIME")
        values = {
            key: _number(row[index], positive=key in {"o", "h", "l", "c"})
            for key, index in (
                ("o", 1),
                ("h", 2),
                ("l", 3),
                ("c", 4),
                ("base_volume", 5),
                ("quote_volume", 7),
                ("taker_buy_base", 9),
                ("taker_buy_quote", 10),
            )
        }
        if not (
            values["l"]
            <= min(values["o"], values["c"])
            <= max(values["o"], values["c"])
            <= values["h"]
        ):
            raise ValueError("INVALID_SPOT_OHLC")
        if not _timestamp(row[8]):
            raise ValueError("INVALID_SPOT_TRADE_COUNT")
        if (
            values["taker_buy_quote"] > values["quote_volume"]
            or values["taker_buy_base"] > values["base_volume"]
            or bool(values["quote_volume"]) != bool(values["base_volume"])
        ):
            raise ValueError("INCONSISTENT_SPOT_VOLUME")
        selected[opened] = {"open_ms": opened, **values}
    if set(selected) != set(range(start, cutoff, HOUR)):
        raise ValueError("SPOT_WINDOW_GAP")
    return [selected[t] for t in sorted(selected)]


def _spot_window(rows, direction):
    total = sum((r["quote_volume"] for r in rows), Decimal(0))
    buy = sum((r["taker_buy_quote"] for r in rows), Decimal(0))
    sell, delta = total - buy, 2 * buy - total
    price_return = (rows[-1]["c"] / rows[0]["o"] - 1) * 100
    return {
        "quote_volume": _text(total),
        "buy_quote": _text(buy),
        "sell_quote": _text(sell),
        "aggressor_delta_quote": _text(delta),
        "imbalance_ratio": _text(delta / total) if total else None,
        "side_adjusted_delta_quote": _text(direction * delta),
        "side_adjusted_imbalance_ratio": _text(direction * delta / total)
        if total
        else None,
        "price_return_pct": _text(price_return),
        "side_adjusted_price_return_pct": _text(direction * price_return),
        "undefined_ratios": [] if total else ["ZERO_QUOTE_VOLUME"],
    }


def _spot(rows, *, start, cutoff, window_hours, direction, **_):
    selected = _spot_rows(rows, start=start, cutoff=cutoff)
    baseline = _spot_window(selected[:window_hours], direction)
    recent = _spot_window(selected[window_hours:], direction)
    denominator = Decimal(baseline["quote_volume"])
    features = {
        "notional_unit": "USDT",
        "baseline": baseline,
        "recent": recent,
        "quote_volume_ratio": (
            _text(Decimal(recent["quote_volume"]) / denominator)
            if denominator
            else None
        ),
        "undefined_ratios": [] if denominator else ["ZERO_BASELINE_QUOTE_VOLUME"],
    }
    normalized = [
        {k: v if k == "open_ms" else _text(v) for k, v in row.items()}
        for row in selected
    ]
    return features, normalized, cutoff - 1


def _open_interest(rows, *, split, cutoff, symbol, **_):
    selected = {}
    for row in rows:
        if not isinstance(row, dict) or not _timestamp(row.get("timestamp")):
            raise ValueError("INVALID_OI_TIMESTAMP")
        timestamp = row["timestamp"]
        if not split <= timestamp <= cutoff:
            continue
        if timestamp % HOUR:
            raise ValueError("MISALIGNED_OI_POINT")
        if timestamp in selected:
            raise ValueError("DUPLICATE_OI_POINT")
        if row.get("symbol") != symbol:
            raise ValueError("OI_SYMBOL_MISMATCH")
        selected[timestamp] = _number(row["sumOpenInterest"])
    if set(selected) != set(range(split, cutoff + HOUR, HOUR)):
        raise ValueError("OI_WINDOW_GAP")
    points = [
        {"timestamp_ms": t, "open_interest_base": _text(selected[t])}
        for t in sorted(selected)
    ]
    opening, closing = selected[split], selected[cutoff]
    change = closing - opening
    return (
        {
            "unit": "BASE_ASSET",
            "start_base": _text(opening),
            "end_base": _text(closing),
            "change_base": _text(change),
            "change_pct": _text(change / opening * 100) if opening else None,
            "hourly_points": points,
            "undefined_ratios": [] if opening else ["ZERO_START_OPEN_INTEREST"],
        },
        points,
        cutoff,
    )


def _funding(rows, *, cutoff, symbol, **_):
    eligible = []
    for row in rows:
        if not isinstance(row, dict) or not _timestamp(row.get("fundingTime")):
            raise ValueError("INVALID_FUNDING_TIMESTAMP")
        timestamp = row["fundingTime"]
        if cutoff - 12 * HOUR <= timestamp < cutoff:
            eligible.append(row)
    if not eligible:
        raise ValueError("FUNDING_MISSING_OR_STALE")
    timestamp = max(r["fundingTime"] for r in eligible)
    latest = [r for r in eligible if r["fundingTime"] == timestamp]
    if len(latest) != 1:
        raise ValueError("DUPLICATE_FUNDING_POINT")
    row = latest[0]
    if row.get("symbol") != symbol:
        raise ValueError("FUNDING_SYMBOL_MISMATCH")
    rate = _number(row["fundingRate"], signed=True)
    if abs(rate) > 1:
        raise ValueError("FUNDING_RATE_BOUND")
    normalized = {"funding_time_ms": timestamp, "actual_rate": _text(rate)}
    return (
        {
            **normalized,
            "actual_rate_pct": _text(rate * 100),
            "age_ms": cutoff - timestamp,
            "rate_basis": "ACTUAL_SETTLED_FUNDING_RATE",
            "funding_interval_hours": None,
        },
        [normalized],
        timestamp,
    )


def _component(envelope, *, source, evaluator, mapping=None, **context):
    receipt = None
    try:
        rows, receipt = _envelope(envelope, source=source, cutoff=context["cutoff"])
        if evaluator is _spot:
            _validate_mapping(mapping, context["symbol"])
        features, normalized, latest = evaluator(rows, **context)
        if receipt["fetched_at_ms"] < latest:
            raise ValueError("FETCH_PRECEDES_SELECTED_EVENT")
    except (ValueError, KeyError) as exc:
        return _unknown(
            str(exc) if isinstance(exc, ValueError) else "MISSING_ROW_FIELD",
            receipt=receipt,
        )
    component_digest = digest(
        canonical(
            {
                "source": source,
                "symbol": context["symbol"],
                "rows": normalized,
            }
        )
    )
    return {
        "status": "OBSERVED",
        "reason": None,
        "features": features,
        "receipt": receipt,
        "input_digest": component_digest,
    }


def analyze_flow_evidence(bundle, *, cutoff_ms, side, window_hours=3):
    """Describe aligned spot, base-unit OI and actual funding independently.

    The accepted bundle is ``mover-flow-public-v1`` / ``BINANCE_LIVE_PUBLIC``
    for one UTC+8 civil day. Component envelopes hold original Binance rows, source,
    status and fetched_at_ms. Spot requires exact current symbol/base/USDT
    mapping. Two equal closed hourly spot windows end at cutoff; OI requires
    every hourly point from the recent window start through cutoff. Funding
    uses the latest settlement strictly before cutoff, no more than 12h old.

    Invalid call configuration raises ValueError. Invalid source data returns
    independent UNKNOWN components. Decimal strings are deterministic under
    caller decimal contexts. Within the bounded source envelopes, future rows
    with valid timestamps cannot change features or their input digest.
    """
    if (
        side not in ("BUY", "SELL")
        or not _timestamp(cutoff_ms)
        or cutoff_ms % HOUR
        or type(window_hours) is not int
        or not 1 <= window_hours <= 12
        or cutoff_ms < 2 * window_hours * HOUR
    ):
        raise ValueError("FLOW_CONFIGURATION_INVALID")
    split = cutoff_ms - window_hours * HOUR
    start = split - window_hours * HOUR
    symbol = bundle.get("symbol") if isinstance(bundle, dict) else None
    report = {
        "version": VERSION,
        "status": "UNKNOWN",
        "reason": None,
        "symbol": symbol if _symbol(symbol) else None,
        "side": side,
        "cutoff_ms": cutoff_ms,
        "window_hours": window_hours,
        "windows": {
            "baseline": {"start_ms": start, "end_ms": split},
            "recent": {"start_ms": split, "end_ms": cutoff_ms},
        },
        "components": {},
        "input_digest": None,
        "limitations": list(_LIMITATIONS),
    }
    reason = None
    if not isinstance(bundle, dict) or bundle.get("version") != BUNDLE_VERSION:
        reason = "BUNDLE_VERSION_INVALID"
    elif bundle.get("source") != "BINANCE_LIVE_PUBLIC":
        reason = "BUNDLE_SOURCE_INVALID"
    elif not _symbol(symbol):
        reason = "BUNDLE_SYMBOL_INVALID"
    else:
        day_start, day_end = bundle.get("start_ms"), bundle.get("end_ms")
        if (
            not _timestamp(day_start)
            or not _timestamp(day_end)
            or (day_start + 8 * HOUR) % DAY
            or day_end - day_start != DAY
            or not day_start <= start < cutoff_ms <= day_end
        ):
            reason = "BUNDLE_DAY_OR_WINDOW_INVALID"
    if reason:
        report["reason"] = reason
        report["components"] = {
            name: _unknown(reason) for name in ("spot", "open_interest", "funding")
        }
        return report
    with localcontext(_CONTEXT):
        context = {
            "start": start,
            "split": split,
            "cutoff": cutoff_ms,
            "window_hours": window_hours,
            "symbol": symbol,
            "direction": Decimal(1 if side == "BUY" else -1),
        }
        for name, source, evaluator in (
            ("spot", "BINANCE_SPOT_LIVE", _spot),
            ("open_interest", "BINANCE_USDM_LIVE", _open_interest),
            ("funding", "BINANCE_USDM_LIVE", _funding),
        ):
            report["components"][name] = _component(
                bundle.get(name),
                source=source,
                evaluator=evaluator,
                mapping=bundle.get("mapping"),
                **context,
            )
    count = sum(c["status"] == "OBSERVED" for c in report["components"].values())
    report["status"] = "OBSERVED" if count == 3 else "PARTIAL" if count else "UNKNOWN"
    report["reason"] = None if count == 3 else "INCOMPLETE_COMPONENT_EVIDENCE"
    if count:
        report["input_digest"] = digest(
            canonical(
                {
                    "version": VERSION,
                    "symbol": symbol,
                    "side": side,
                    "cutoff_ms": cutoff_ms,
                    "window_hours": window_hours,
                    "components": {
                        name: {
                            "status": component["status"],
                            "reason": component["reason"],
                            "input_digest": component["input_digest"],
                        }
                        for name, component in report["components"].items()
                    },
                }
            )
        )
    return report
