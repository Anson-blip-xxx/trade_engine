"""Point-in-time public positioning descriptors, not a prediction or permit.

Row timestamps are event times, not proof of historical publication. The first
local receipt is always retained. Reconstructed earlier cutoffs are explicitly
retrospective; future rows never contribute to a selected-evidence digest.
"""

from decimal import Decimal, InvalidOperation, localcontext

from v2_core.evidence import canonical, digest

VERSION = "positioning-observation-v1"
SOURCE = "BINANCE_USDM_LIVE_PUBLIC"
HOUR = 3_600_000
STEP = 300_000
ENDPOINTS = {
    "hourly_klines": "/fapi/v1/klines",
    "open_interest": "/futures/data/openInterestHist",
    "global_accounts": "/futures/data/globalLongShortAccountRatio",
    "top_accounts": "/futures/data/topLongShortAccountRatio",
    "top_positions": "/futures/data/topLongShortPositionRatio",
    "funding_history": "/fapi/v1/fundingRate",
    "premium_index": "/fapi/v1/premiumIndex",
}


def symbol(value):
    if (
        not isinstance(value, str)
        or not 5 <= len(value) <= 44
        or not value.endswith("USDT")
        or not all(c.isalnum() or c == "_" for c in value[:-4])
    ):
        raise ValueError("POSITIONING_SYMBOL_INVALID")
    return value


def _ms(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("INVALID_EVENT_TIME")
    return value


def _number(value, *, low=None, high=None):
    if type(value) not in (str, int) or len(str(value)) > 80:
        raise ValueError("INVALID_NUMBER")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("INVALID_NUMBER") from None
    if (
        not number.is_finite()
        or abs(number) > Decimal("1e30")
        or (number and number.adjusted() < -30)
        or (low is not None and number < low)
        or (high is not None and number > high)
    ):
        raise ValueError("INVALID_NUMBER")
    return number


def _text(value):
    return "0" if value == 0 else format(value.normalize(), "f")


def _unknown(reason):
    return {
        "status": "UNKNOWN",
        "reason": reason,
        "features": None,
        "input_digest": None,
        "historical_receipt_proven": False,
    }


def _change(first, last):
    a, b = Decimal(first), Decimal(last)
    return {
        "start": first,
        "end": last,
        "change": _text(b - a),
        "change_pct": _text((b / a - 1) * 100) if a else None,
        "zero_start": a == 0,
    }


def _points(name, raw, target, cutoff):
    if not isinstance(raw, list) or not 0 < len(raw) <= 80:
        raise ValueError("INVALID_ROWS_BOUND")
    end = cutoff // STEP * STEP
    selected = []
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("INVALID_ROW")  # noqa: TRY004 - stable research reason
        t = _ms(row.get("timestamp"))
        # Match the collector's publication-lag buffer, not an assumption that
        # the newest five-minute statistic has already been published.
        if not end - 6 * HOUR - 2 * STEP <= t <= end:
            continue
        if row.get("symbol") != target or t % STEP:
            raise ValueError("EVENT_SCOPE_MISMATCH")
        point = {"event_ms": t}
        if name == "open_interest":
            point.update(
                base=_text(_number(row["sumOpenInterest"], low=0)),
                value_usdt=_text(_number(row["sumOpenInterestValue"], low=0)),
            )
        else:
            long = _number(row["longAccount"], low=0, high=1)
            short = _number(row["shortAccount"], low=0, high=1)
            if abs(long + short - 1) > Decimal("0.0002"):
                raise ValueError("SHARES_DO_NOT_SUM_TO_ONE")
            point.update(
                long_share=_text(long),
                short_share=_text(short),
                reported_long_short_ratio=_text(_number(row["longShortRatio"], low=0)),
            )
        selected.append(point)
    selected.sort(key=lambda row: row["event_ms"])
    if not selected:
        raise ValueError("NO_ELIGIBLE_OBSERVATIONS")
    if len({r["event_ms"] for r in selected}) != len(selected):
        raise ValueError("DUPLICATE_EVENT")
    latest = selected[-1]
    if cutoff - latest["event_ms"] > 2 * STEP:
        raise ValueError("STALE_STATISTICS")
    features = {
        "latest": latest,
        "age_ms": cutoff - latest["event_ms"],
        "basis": "BASE_AND_REPORTED_USDT_NOT_CAPITAL_INFLOW"
        if name == "open_interest"
        else "POSITION_SHARE"
        if name == "top_positions"
        else "ACCOUNT_SHARE",
    }
    by_time = {row["event_ms"]: row for row in selected}
    for hours in (3, 6):
        times = list(
            range(latest["event_ms"] - hours * HOUR, latest["event_ms"] + STEP, STEP)
        )
        if any(t not in by_time for t in times):
            features[f"window_{hours}h"] = _unknown("INCOMPLETE_STATISTICS_WINDOW")
            continue
        first = by_time[times[0]]
        window = {
            "status": "OBSERVED",
            "start_ms": times[0],
            "end_ms": times[-1],
            "points": len(times),
        }
        if name == "open_interest":
            window.update(
                base=_change(first["base"], latest["base"]),
                value_usdt=_change(first["value_usdt"], latest["value_usdt"]),
            )
        else:
            window.update(
                long_share_change_pp=_text(
                    (Decimal(latest["long_share"]) - Decimal(first["long_share"])) * 100
                ),
                short_share_change_pp=_text(
                    (Decimal(latest["short_share"]) - Decimal(first["short_share"]))
                    * 100
                ),
            )
        features[f"window_{hours}h"] = window
    return selected, features, latest["event_ms"]


def _hourly(raw, target, cutoff):
    if not isinstance(raw, list) or not 0 < len(raw) <= 80:
        raise ValueError("INVALID_ROWS_BOUND")
    end = cutoff // HOUR * HOUR
    selected = []
    for row in raw:
        if not isinstance(row, list) or not row:
            raise ValueError("INVALID_ROW")
        t = _ms(row[0])
        if not end - 72 * HOUR <= t < end:
            continue
        if len(row) < 11 or t % HOUR or _ms(row[6]) != t + HOUR - 1:
            raise ValueError("INVALID_CLOSED_HOUR")
        o, h, l, c = (_number(v, low=Decimal("1e-30")) for v in row[1:5])
        v, quote, buy, buy_quote = (_number(row[i], low=0) for i in (5, 7, 9, 10))
        if not l <= min(o, c) <= max(o, c) <= h or buy > v or buy_quote > quote:
            raise ValueError("INVALID_CANDLE_OR_FLOW")
        selected.append(
            {
                "event_ms": t + HOUR,
                "open_ms": t,
                "open": _text(o),
                "high": _text(h),
                "low": _text(l),
                "close": _text(c),
                "base_volume": _text(v),
                "quote_volume": _text(quote),
                "taker_buy_base": _text(buy),
                "taker_buy_quote": _text(buy_quote),
            }
        )
    selected.sort(key=lambda row: row["open_ms"])
    if not selected:
        raise ValueError("NO_ELIGIBLE_OBSERVATIONS")
    if len({r["open_ms"] for r in selected}) != len(selected):
        raise ValueError("DUPLICATE_EVENT")
    by_time = {row["open_ms"]: row for row in selected}
    features = {
        "closed_hour_end_ms": end,
        "available_bars": len(selected),
        "complete_72h": len(selected) == 72,
        "age_ms": cutoff - selected[-1]["event_ms"],
    }
    for hours in (3, 6):
        times = list(range(end - hours * HOUR, end, HOUR))
        if any(t not in by_time for t in times):
            features[f"window_{hours}h"] = _unknown("INCOMPLETE_CANDLE_WINDOW")
            continue
        rows = [by_time[t] for t in times]
        volume = sum(Decimal(r["base_volume"]) for r in rows)
        buy = sum(Decimal(r["taker_buy_base"]) for r in rows)
        ret = (Decimal(rows[-1]["close"]) / Decimal(rows[0]["open"]) - 1) * 100
        features[f"window_{hours}h"] = {
            "status": "OBSERVED",
            "start_ms": times[0],
            "end_ms": end,
            "return_pct": _text(ret),
            "direction": "UP" if ret > 0 else "DOWN" if ret < 0 else "FLAT",
            "base_volume": _text(volume),
            "taker_buy_base": _text(buy),
            "taker_buy_fraction": _text(buy / volume) if volume else None,
            "zero_volume": volume == 0,
        }
    return selected, features, selected[-1]["event_ms"]


def _funding(raw, target, cutoff):
    if not isinstance(raw, list) or not 0 < len(raw) <= 100:
        raise ValueError("INVALID_ROWS_BOUND")
    selected = []
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("INVALID_ROW")  # noqa: TRY004 - stable research reason
        t = _ms(row.get("fundingTime"))
        if not cutoff - 24 * HOUR <= t <= cutoff:
            continue
        if row.get("symbol") != target:
            raise ValueError("EVENT_SCOPE_MISMATCH")
        selected.append(
            {
                "event_ms": t,
                "actual_settled_rate": _text(
                    _number(row["fundingRate"], low=-1, high=1)
                ),
            }
        )
    selected.sort(key=lambda row: row["event_ms"])
    if not selected:
        raise ValueError("NO_ELIGIBLE_OBSERVATIONS")
    if len({r["event_ms"] for r in selected}) != len(selected):
        raise ValueError("DUPLICATE_EVENT")
    return (
        selected,
        {
            "basis": "ACTUAL_SETTLED_FUNDING",
            "latest": selected[-1],
            "settled_rates": selected,
            "previous_interval_ms": selected[-1]["event_ms"] - selected[-2]["event_ms"]
            if len(selected) > 1
            else None,
            "future_interval_known": False,
            "age_ms": cutoff - selected[-1]["event_ms"],
        },
        selected[-1]["event_ms"],
    )


def _premium(raw, target, cutoff):
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise ValueError("INVALID_PREMIUM")
    raw = raw[0]
    t = _ms(raw.get("time"))
    if t > cutoff:
        raise ValueError("NO_ELIGIBLE_OBSERVATIONS")
    if raw.get("symbol") != target:
        raise ValueError("EVENT_SCOPE_MISMATCH")
    if cutoff - t > STEP:
        raise ValueError("STALE_PREMIUM")
    nxt = _ms(raw.get("nextFundingTime"))
    if not t < nxt <= t + 24 * HOUR:
        raise ValueError("INVALID_NEXT_FUNDING_TIME")
    point = {
        "event_ms": t,
        "mark_price": _text(_number(raw["markPrice"], low=Decimal("1e-30"))),
        "index_price": _text(_number(raw["indexPrice"], low=Decimal("1e-30"))),
        "quoted_rate": _text(_number(raw["lastFundingRate"], low=-1, high=1)),
        "next_funding_ms": nxt,
    }
    return (
        [point],
        dict(
            **point,
            age_ms=cutoff - t,
            basis="PREMIUM_INDEX_QUOTE_NOT_ACTUAL_SETTLEMENT",
            future_actual_rate_known=False,
        ),
        t,
    )


def describe(bundle, *, cutoff_ms, as_of_ms):
    cutoff, asof = _ms(cutoff_ms), _ms(as_of_ms)
    if (
        not isinstance(bundle, dict)
        or bundle.get("version") != VERSION
        or bundle.get("source") != SOURCE
        or bundle.get("source_host") != "fapi.binance.com"
        or not isinstance(bundle.get("components"), dict)
        or cutoff > asof
    ):
        raise ValueError("POSITIONING_BUNDLE_INVALID")
    target = symbol(bundle.get("symbol"))
    started, finished = (
        _ms(bundle.get("started_at_ms")),
        _ms(bundle.get("finished_at_ms")),
    )
    if finished < started:
        raise ValueError("POSITIONING_BUNDLE_CLOCK")
    components, proofs = {}, {}
    with localcontext() as ctx:
        ctx.prec = 60
        for name, endpoint in ENDPOINTS.items():
            try:
                envelope = bundle["components"].get(name)
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("source") != SOURCE
                    or envelope.get("source_host") != "fapi.binance.com"
                    or envelope.get("endpoint") != endpoint
                ):
                    raise ValueError("COMPONENT_SOURCE_UNCONFIRMED")
                if envelope.get("status") != "OBSERVED":
                    raise ValueError("COMPONENT_UNAVAILABLE")
                fetched = _ms(envelope.get("fetched_at_ms"))
                if not started <= fetched <= finished:
                    raise ValueError("INVALID_RECEIPT_TIME")
                if fetched > asof:
                    raise ValueError("NOT_AVAILABLE_AS_OF")
                raw = envelope.get("rows")
                encoded = canonical({"rows": raw})
                if len(encoded.encode()) > 256 * 1024 or envelope.get(
                    "sha256"
                ) != digest(encoded):
                    raise ValueError("COMPONENT_DIGEST_MISMATCH")
                if name == "hourly_klines":
                    selected, features, event = _hourly(raw, target, cutoff)
                elif name == "funding_history":
                    selected, features, event = _funding(raw, target, cutoff)
                elif name == "premium_index":
                    selected, features, event = _premium(raw, target, cutoff)
                else:
                    selected, features, event = _points(name, raw, target, cutoff)
                if event > fetched:
                    raise ValueError("EVENT_AFTER_RECEIPT")
                proof = {
                    "symbol": target,
                    "source": SOURCE,
                    "endpoint": endpoint,
                    "selected": selected,
                    "first_seen_ms": fetched,
                }
                proofs[name] = proof
                components[name] = {
                    "status": "OBSERVED",
                    "reason": None,
                    "source": SOURCE,
                    "endpoint": endpoint,
                    "features": features,
                    "first_seen_ms": fetched,
                    "availability": "TIMELY_FIRST_SEEN"
                    if fetched <= cutoff
                    else "RETROSPECTIVE",
                    "historical_receipt_proven": False,
                    "first_seen_after_event_ms": fetched - event,
                    "event_ms": event,
                    "input_digest": digest(canonical(proof)),
                }
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                # Never echo raw payload/credential/transport text in research.
                reason = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    and str(exc).isupper()
                    and len(str(exc)) < 80
                    else "INVALID_COMPONENT"
                )
                components[name] = _unknown(reason)
    count = sum(c["status"] == "OBSERVED" for c in components.values())
    return {
        "version": VERSION,
        "source": SOURCE,
        "symbol": target,
        "cutoff_ms": cutoff,
        "as_of_ms": asof,
        "status": "OBSERVED"
        if count == len(ENDPOINTS)
        else "PARTIAL"
        if count
        else "UNKNOWN",
        "components": components,
        "input_digest": digest(
            canonical(
                {
                    "version": VERSION,
                    "symbol": target,
                    "cutoff_ms": cutoff,
                    "proofs": proofs,
                }
            )
        ),
        "historical_receipt_proven": False,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "limitations": [
            "TIMELY_FIRST_SEEN means known at this snapshot, not at earlier event times.",
            "Historical statistic publication time is unknown; retrospective rows cannot authorize an earlier decision.",
            "OI is two-sided outstanding contracts, not identified new shorts or capital inflow.",
            "Account and position shares are different populations; participant identity and intent are unknown.",
            "Premium-index quotes and future schedule are not actual future funding settlements.",
            "No trade recommendation, model probability, strategy return or automatic policy change.",
        ],
    }
