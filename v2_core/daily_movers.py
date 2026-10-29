"""Fixed UTC+8 civil-day mover research; no I/O and no execution permissions.

Ranking is an ex-post sample selection, never evidence that a coin was
predictable. Source gaps, new listings and unclosed days stay explicitly unknown.
Prices/amounts are decimal strings; no binary float enters the calculations.
"""

import re
from collections import Counter
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

VERSION = "daily-movers-utc8-v1"
MINUTE = 60_000
DAY = 1440 * MINUTE
ZONE = ZoneInfo("Asia/Shanghai")
_CONTEXT = Context(prec=60, rounding=ROUND_HALF_EVEN)
_ENGINE_SYMBOL = re.compile(r"[A-Z0-9]{1,32}USDT")
_MAX_MS = 2**53 - 1


def _ms(value):
    if type(value) is not int or not 0 <= value <= _MAX_MS:
        raise ValueError("INVALID_TIMESTAMP")
    return value


def _valid_symbol(value):
    # Public Binance contracts may use Unicode names. Engine compatibility is
    # a separate outcome, not a reason to hide an important market mover.
    return (
        isinstance(value, str)
        and 5 <= len(value) <= 44
        and value.endswith("USDT")
        and all(char.isalnum() or char == "_" for char in value[:-4])
    )


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("INVALID_DECIMAL")  # noqa: TRY004 - stable research reason
    text = str(value)
    if len(text) > 100:
        raise ValueError("DECIMAL_BOUND")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("INVALID_DECIMAL") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("INVALID_DECIMAL")
    if result and not -40 <= result.adjusted() <= 40:
        raise ValueError("DECIMAL_BOUND")
    return result


def _signed_number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("INVALID_DECIMAL")  # noqa: TRY004 - stable research reason
    text = str(value)
    if len(text) > 100:
        raise ValueError("DECIMAL_BOUND")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("INVALID_DECIMAL") from exc
    if not result.is_finite():
        raise ValueError("INVALID_DECIMAL")
    _number(result.copy_abs())
    return result


def _text(value):
    if value == 0:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def previous_day_window(now_ms):
    """Previous *completed* UTC+8 date, not the trailing 24 hours."""
    local = datetime.fromtimestamp(_ms(now_ms) // 1000, ZONE)
    end = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    return {
        "date": start.date().isoformat(),
        "timezone": "Asia/Shanghai",
        "start_ms": int(start.timestamp()) * 1000,
        "end_ms": int(end.timestamp()) * 1000,
    }


def _window(start_ms, end_ms, as_of_ms):
    for value in (start_ms, end_ms, as_of_ms):
        _ms(value)
    if end_ms - start_ms != DAY or (start_ms + 8 * 60 * MINUTE) % DAY:
        raise ValueError("NOT_UTC8_CIVIL_DAY")


def _unknown(reason, **extra):
    return {"version": VERSION, "status": "UNKNOWN", "reason": reason, **extra}


def _closed_rows(rows, *, start_ms, end_ms, as_of_ms, interval_minutes):
    _window(start_ms, end_ms, as_of_ms)
    if type(interval_minutes) is not int or interval_minutes not in (1, 5, 15, 30, 60):
        raise ValueError("UNSUPPORTED_SOURCE_INTERVAL")
    if as_of_ms < end_ms:
        return None, _unknown("DAY_NOT_CLOSED")
    if not isinstance(rows, (list, tuple)) or len(rows) > 3000:
        return None, _unknown("SOURCE_ROWS_BOUND")
    width, selected, ignored = interval_minutes * MINUTE, {}, 0
    try:
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("INVALID_BAR")  # noqa: TRY004 - stable research reason
            t = _ms(row.get("t"))
            if t < start_ms or t >= end_ms:
                ignored += 1  # future prices cannot affect a completed-day report
                continue
            if (t - start_ms) % width:
                raise ValueError("MISALIGNED_BAR")
            if t in selected:
                raise ValueError("DUPLICATE_BAR")
            values = {
                key: _number(row[key], positive=key != "v")
                for key in ("o", "h", "l", "c", "v")
            }
            if (
                not values["l"]
                <= min(values["o"], values["c"])
                <= max(values["o"], values["c"])
                <= values["h"]
            ):
                raise ValueError("INVALID_OHLC")
            selected[t] = {"t": t, **values}
    except (ValueError, KeyError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "MISSING_BAR_FIELD"
        return None, _unknown(reason)
    expected = DAY // width
    if len(selected) != expected:
        return None, _unknown(
            "MISSING_OR_PARTIAL_DAY",
            source_count=len(selected),
            expected_count=expected,
        )
    return [selected[t] for t in sorted(selected)], {"ignored_outside_day": ignored}


def summarize_day(rows, *, symbol, start_ms, end_ms, as_of_ms, interval_minutes=1):
    """Attest a whole day from contiguous, closed 1/5/15/30/60-minute candles."""
    if not _valid_symbol(symbol):
        raise ValueError("INVALID_USDT_SYMBOL")
    with localcontext(_CONTEXT):
        selected, metadata = _closed_rows(
            rows,
            start_ms=start_ms,
            end_ms=end_ms,
            as_of_ms=as_of_ms,
            interval_minutes=interval_minutes,
        )
        base = {"symbol": symbol, "start_ms": start_ms, "end_ms": end_ms}
        if selected is None:
            return {**base, **metadata, "complete": False}
        return {
            **base,
            "version": VERSION,
            "status": "COMPLETE",
            "complete": True,
            "source_interval_minutes": interval_minutes,
            "source_count": len(selected),
            "o": _text(selected[0]["o"]),
            "h": _text(max(row["h"] for row in selected)),
            "l": _text(min(row["l"] for row in selected)),
            "c": _text(selected[-1]["c"]),
            "v": _text(sum((row["v"] for row in selected), Decimal(0))),
            **metadata,
        }


def _summary_values(row, *, start_ms, end_ms):
    if row.get("start_ms") != start_ms or row.get("end_ms") != end_ms:
        raise ValueError("SUMMARY_WINDOW_MISMATCH")
    interval = row.get("source_interval_minutes")
    if (
        row.get("complete") is not True
        or type(interval) is not int
        or interval not in (1, 5, 15, 30, 60)
        or type(row.get("source_count")) is not int
        or row["source_count"] != 1440 // interval
    ):
        raise ValueError("SUMMARY_COMPLETENESS_UNPROVEN")
    values = {
        key: _number(row[key], positive=key != "v") for key in ("o", "h", "l", "c", "v")
    }
    if (
        not values["l"]
        <= min(values["o"], values["c"])
        <= max(values["o"], values["c"])
        <= values["h"]
    ):
        raise ValueError("INVALID_OHLC")
    return values


def rank_daily_movers(
    universe,
    summaries,
    *,
    start_ms,
    end_ms,
    as_of_ms,
    top_n=10,
    universe_observed_at_ms=None,
):
    """Rank strictly positive/negative fixed-day returns; ties use symbol.

    Universe discovery after the day is explicitly survivorship-limited. Caller
    must preserve historical snapshots to establish a genuine historical universe.
    A summary is accepted only as the output contract of summarize_day.
    """
    _window(start_ms, end_ms, as_of_ms)
    if type(top_n) is not int or not 1 <= top_n <= 50:
        raise ValueError("TOP_N_BOUND")
    if not isinstance(universe, (list, tuple)) or len(universe) > 3000:
        raise ValueError("UNIVERSE_BOUND")
    if not isinstance(summaries, (list, tuple)) or len(summaries) > 3000:
        raise ValueError("SUMMARIES_BOUND")
    if universe_observed_at_ms is not None:
        _ms(universe_observed_at_ms)
        if universe_observed_at_ms > as_of_ms:
            raise ValueError("FUTURE_UNIVERSE_SNAPSHOT")
    if as_of_ms < end_ms:
        return _unknown("DAY_NOT_CLOSED", winners=[], losers=[], exclusions=[])
    rows, duplicate_summaries = {}, set()
    for row in summaries:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
            raise ValueError("INVALID_SUMMARY_IDENTITY")  # noqa: TRY004
        if row["symbol"] in rows:
            duplicate_summaries.add(row["symbol"])
        rows[row["symbol"]] = row
    symbols = [
        item.get("symbol") if isinstance(item, dict) else None for item in universe
    ]
    if any(not _valid_symbol(s) for s in symbols):
        raise ValueError("INVALID_UNIVERSE_SYMBOL")
    duplicates = {s for s, count in Counter(symbols).items() if count > 1}
    exclusions, eligible, seen = [], [], set()
    with localcontext(_CONTEXT):
        for item in sorted(universe, key=lambda item: item["symbol"]):
            symbol = item["symbol"]
            if symbol in seen:
                continue
            seen.add(symbol)
            try:
                if symbol in duplicates:
                    raise ValueError("DUPLICATE_UNIVERSE_SYMBOL")
                if (
                    item.get("quoteAsset") != "USDT"
                    or item.get("contractType") != "PERPETUAL"
                ):
                    raise ValueError("OUTSIDE_USDT_PERPETUAL_UNIVERSE")
                if (
                    item.get("onboardDate") is not None
                    and _ms(item["onboardDate"]) > start_ms
                ):
                    raise ValueError("NEW_LISTING_PARTIAL_DAY")
                if (
                    item.get("deliveryDate") is not None
                    and _ms(item["deliveryDate"]) < end_ms
                ):
                    raise ValueError("DELISTING_PARTIAL_DAY")
                if symbol in duplicate_summaries:
                    raise ValueError("DUPLICATE_SUMMARY")
                if symbol not in rows:
                    raise ValueError("MISSING_DAILY_SUMMARY")
                values = _summary_values(rows[symbol], start_ms=start_ms, end_ms=end_ms)
                change = (values["c"] / values["o"] - 1) * 100
                eligible.append(
                    {
                        "symbol": symbol,
                        "engine_symbol_supported": bool(
                            _ENGINE_SYMBOL.fullmatch(symbol)
                        ),
                        "return_pct": _text(change),
                        "open": _text(values["o"]),
                        "close": _text(values["c"]),
                        "high": _text(values["h"]),
                        "low": _text(values["l"]),
                        "base_volume": _text(values["v"]),
                    }
                )
            except (ValueError, KeyError) as exc:
                exclusions.append(
                    {
                        "symbol": symbol,
                        "reason": str(exc)
                        if isinstance(exc, ValueError)
                        else "MISSING_SUMMARY_FIELD",
                    }
                )
        winners = sorted(
            (r for r in eligible if Decimal(r["return_pct"]) > 0),
            key=lambda r: (-Decimal(r["return_pct"]), r["symbol"]),
        )[:top_n]
        losers = sorted(
            (r for r in eligible if Decimal(r["return_pct"]) < 0),
            key=lambda r: (Decimal(r["return_pct"]), r["symbol"]),
        )[:top_n]
    historical = (
        universe_observed_at_ms is not None and universe_observed_at_ms <= start_ms
    )
    return {
        "version": VERSION,
        "status": "COMPLETE" if not exclusions and historical else "PARTIAL_COVERAGE",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "timezone": "Asia/Shanghai",
        "return_basis": "FIXED_DAY_CLOSE_DIV_OPEN_MINUS_ONE",
        "ranking_use": "EX_POST_RESEARCH_SELECTION_NOT_PREDICTION",
        "universe_basis": "HISTORICAL_SNAPSHOT"
        if historical
        else "CURRENT_DISCOVERY_SURVIVORSHIP_LIMITED",
        "universe_observed_at_ms": universe_observed_at_ms,
        "coverage": {
            "discovered": len(seen),
            "eligible": len(eligible),
            "excluded": len(exclusions),
            "zero_return": sum(Decimal(r["return_pct"]) == 0 for r in eligible),
        },
        "winners": winners,
        "losers": losers,
        "eligible_summaries": eligible,
        "exclusions": exclusions,
    }


def analyze_intraday_path(
    rows,
    *,
    start_ms,
    end_ms,
    as_of_ms,
    side="BUY",
    entry_ms=None,
    entry_price=None,
    realized_net=None,
    market_source="UNKNOWN",
    entry_price_source=None,
):
    """Describe a complete 1m path and, optionally, strictly post-entry extrema.

    Entry-minute highs/lows are excluded because OHLC does not reveal whether
    they occurred before the fill. Even later extrema are hindsight price bounds,
    not achievable returns: fees, funding, depth and executable size are unknown.
    Actual realized net is a caller-supplied accounting label, never synthesized.
    """
    if side not in ("BUY", "SELL"):
        raise ValueError("INVALID_SIDE")
    if (entry_ms is None) != (entry_price is None):
        raise ValueError("INCOMPLETE_ENTRY_REFERENCE")
    if not isinstance(market_source, str) or not 1 <= len(market_source) <= 80:
        raise ValueError("INVALID_MARKET_SOURCE")
    if entry_price_source is not None and (
        not isinstance(entry_price_source, str)
        or not 1 <= len(entry_price_source) <= 80
    ):
        raise ValueError("INVALID_ENTRY_SOURCE")
    if entry_ms is not None:
        _ms(entry_ms)
        _number(entry_price, positive=True)
    actual = None if realized_net is None else _text(_signed_number(realized_net))
    with localcontext(_CONTEXT):
        selected, metadata = _closed_rows(
            rows,
            start_ms=start_ms,
            end_ms=end_ms,
            as_of_ms=as_of_ms,
            interval_minutes=1,
        )
        if selected is None:
            return {
                **metadata,
                "features": None,
                "post_entry": None,
                "actual_realized_net": actual,
            }
        opening, closing = selected[0]["o"], selected[-1]["c"]
        high = max(selected, key=lambda r: r["h"])
        low = min(selected, key=lambda r: r["l"])
        direction = Decimal(1 if side == "BUY" else -1)
        volume = sum((r["v"] for r in selected), Decimal(0))
        favorable, streak, longest = 0, 0, 0
        previous = selected[0]["c"]
        reference, best_run = previous, Decimal(0)
        for row in selected[1:]:
            close = row["c"]
            if direction * (close - previous) > 0:
                favorable += 1
                streak += 1
                longest = max(longest, streak)
            else:
                streak = 0
            best_run = max(best_run, direction * (close / reference - 1) * 100)
            reference = (
                min(reference, close) if side == "BUY" else max(reference, close)
            )
            previous = close
        hourly = []
        for offset in range(0, 1440, 60):
            hour = selected[offset : offset + 60]
            hourly.append(
                {
                    "start_ms": hour[0]["t"],
                    "return_pct": _text((hour[-1]["c"] / hour[0]["o"] - 1) * 100),
                    "base_volume": _text(sum((r["v"] for r in hour), Decimal(0))),
                }
            )
        spread = high["h"] - low["l"]
        features = {
            "side": side,
            "fixed_day_return_pct": _text((closing / opening - 1) * 100),
            "range_pct_of_open": _text(spread / opening * 100),
            "high": _text(high["h"]),
            "high_minute_ms": high["t"],
            "low": _text(low["l"]),
            "low_minute_ms": low["t"],
            "close_location_in_range_pct": None
            if not spread
            else _text((closing - low["l"]) / spread * 100),
            "favorable_close_step_pct": _text(Decimal(favorable) / 1439 * 100),
            "longest_favorable_close_streak_minutes": longest,
            "best_ordered_close_run_pct": _text(best_run),
            "base_volume": _text(volume),
            "largest_hour_volume_share_pct": None
            if not volume
            else _text(max(Decimal(r["base_volume"]) for r in hourly) / volume * 100),
            "hourly": hourly,
        }
        post = None
        if entry_ms is not None:
            if market_source == "UNKNOWN" or entry_price_source != market_source:
                post = _unknown("PRICE_SOURCES_NOT_COMPARABLE")
            elif not start_ms <= entry_ms < end_ms:
                post = _unknown("ENTRY_OUTSIDE_DAY")
            else:
                # Always exclude the containing minute, including an aligned
                # timestamp: a minute bar does not establish fill/quote ordering.
                first = (entry_ms // MINUTE + 1) * MINUTE
                after = [r for r in selected if r["t"] >= first]
                if not after:
                    post = _unknown("NO_FULL_POST_ENTRY_MINUTE")
                else:
                    price = _number(entry_price, positive=True)
                    favorable_row = (
                        max(after, key=lambda r: r["h"])
                        if side == "BUY"
                        else min(after, key=lambda r: r["l"])
                    )
                    adverse_row = (
                        min(after, key=lambda r: r["l"])
                        if side == "BUY"
                        else max(after, key=lambda r: r["h"])
                    )
                    favorable_price = favorable_row["h" if side == "BUY" else "l"]
                    adverse_price = adverse_row["l" if side == "BUY" else "h"]
                    post = {
                        "status": "OBSERVED_DESCRIPTIVE_BOUND",
                        "entry_minute_excluded": True,
                        "first_observed_minute_ms": first,
                        "favorable_price_excursion_pct": _text(
                            direction * (favorable_price / price - 1) * 100
                        ),
                        "adverse_price_excursion_pct": _text(
                            direction * (adverse_price / price - 1) * 100
                        ),
                        "favorable_minute_ms": favorable_row["t"],
                        "adverse_minute_ms": adverse_row["t"],
                        "actual_or_achievable_profit": False,
                    }
        return {
            "version": VERSION,
            "status": "OBSERVED",
            "features": features,
            "market_source": market_source,
            "entry_price_source": entry_price_source,
            "post_entry": post,
            "actual_realized_net": actual,
            "realized_net_basis": "CALLER_ACCOUNTING_LABEL_NOT_VERIFIED_HERE"
            if actual is not None
            else "UNKNOWN",
            "limitations": [
                "EX_POST_WINNER_SELECTION_NOT_ENTRY_SIGNAL",
                "INTRAMINUTE_SEQUENCE_UNKNOWN",
                "OHLC_EXTREMA_NOT_EXECUTABLE_PROFIT",
                "NO_DEPTH_SLIPPAGE_FUNDING_OR_FEES_IN_PRICE_EXCURSIONS",
            ],
            **metadata,
        }
