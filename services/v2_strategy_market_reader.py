"""Bounded, read-only SANDBOX closed-minute archive facts for candidate research.

This port has no catalog, credentials, account state, writes or order authority.
It requires a dedicated read-only ClickHouse client with query_limit=0,
query_retries=0, a short receive timeout and complete server-setting metadata.
Use a dedicated profile whose immutable limits exactly match the requested
settings. The existing tradev2shadow profile is NOT compatible with the default
limits here. Unknown/read-only-mismatched settings fail even if the client's
global invalid_setting_action would drop them. No network probe is made here.

The composition owner must also configure a short connect_timeout (for example
1 second); the client exposes no stable public metadata for verifying that
transport setting. Server query settings and socket inactivity timeouts are NOT
a hard wall-clock deadline. The caller owns the client and enclosing bounded
process supervision; this module does not change a server profile or account.

Missing minutes and conflicting duplicates are unknown facts, never NO_SETUP.
No LIMIT, aggregation or overflow ``break`` may conceal a conflicting raw row.
The result records observation time, not proof of historical availability.
"""

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, localcontext

from v2_core.evidence import digest
from v2_core.ingress import milliseconds
from v2_core.ingress import symbol as validate_symbol
from v2_core.quant_indicators import MINUTE, Bar, decimal

_MAX_MINUTES = 1440
_MAX_RAW_ROWS = 10000
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_MAX_ROW_BYTES = 16384
_BAR_FIELDS = frozenset({"t", "o", "h", "l", "c", "v", "tbv"})


def _bounded_int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("CANDLE_READ_LIMIT_INVALID")
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("CANDLE_ARCHIVE_PAYLOAD_INVALID")
        result[key] = value
    return result


def _json(payload):
    try:
        return json.loads(payload, object_pairs_hook=_object)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("CANDLE_ARCHIVE_PAYLOAD_INVALID") from None


def _encode_bars(bars):
    return json.dumps(bars, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validate_bars(bars, closed_at_ms):
    if type(bars) is not list or not 1 <= len(bars) <= _MAX_MINUTES:
        raise ValueError("CANDLE_HISTORY_INCOMPLETE")
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        for index, row in enumerate(bars):
            if (
                type(row) is not dict
                or set(row) != _BAR_FIELDS
                or type(row["t"]) is not int
                or row["t"] != closed_at_ms - (len(bars) - index) * MINUTE
                or row["t"] < 0
                or any(type(row[key]) is not str for key in _BAR_FIELDS - {"t"})
            ):
                raise ValueError("CANDLE_ARCHIVE_BAR_INVALID")
            try:
                bar = Bar(**{key: row[key] for key in ("t", "o", "h", "l", "c", "v")})
                taker = decimal(row["tbv"])
                if not 0 <= taker <= bar.v:
                    raise ValueError("CANDLE_ARCHIVE_BAR_INVALID")
            except (ArithmeticError, ValueError):
                raise ValueError("CANDLE_ARCHIVE_BAR_INVALID") from None


@dataclass(frozen=True)
class ClosedMarketBars:
    """Detached facts; constructing this value does not authenticate a source."""

    symbol: str
    environment: str
    closed_at_ms: int
    available_at_ms: int
    bars_json: str

    def __post_init__(self):
        validate_symbol(self.symbol)
        milliseconds(self.closed_at_ms)
        milliseconds(self.available_at_ms)
        if (
            self.environment != "SANDBOX"
            or self.closed_at_ms % MINUTE
            or self.closed_at_ms > self.available_at_ms
        ):
            raise ValueError("CANDLE_ARCHIVE_SCOPE")
        if (
            type(self.bars_json) is not str
            or len(self.bars_json.encode()) > _MAX_PAYLOAD_BYTES
        ):
            raise ValueError("CANDLE_ARCHIVE_PAYLOAD_BOUND")
        bars = _json(self.bars_json)
        _validate_bars(bars, self.closed_at_ms)
        object.__setattr__(self, "bars_json", _encode_bars(bars))

    @property
    def bars(self):
        """A fresh oldest-first list; consumers cannot mutate frozen evidence."""
        return json.loads(self.bars_json)


class ClosedMarketReader:
    def __init__(
        self,
        client,
        *,
        clock_ms,
        max_raw_rows=2880,
        max_payload_bytes=4 * 1024 * 1024,
        query_timeout_seconds=5,
    ):
        if not callable(clock_ms) or not callable(getattr(client, "query", None)):
            raise TypeError("CANDLE_READ_PORT_REQUIRED")
        self.client = client
        self.clock_ms = clock_ms
        self.max_raw_rows = _bounded_int(max_raw_rows, 1, _MAX_RAW_ROWS)
        self.max_payload_bytes = _bounded_int(
            max_payload_bytes, 1024, _MAX_PAYLOAD_BYTES
        )
        self.query_timeout_seconds = _bounded_int(query_timeout_seconds, 1, 10)
        self._check_client()

    def _settings(self):
        return {
            "readonly": 1,
            "max_threads": 1,
            "max_execution_time": _bounded_int(self.query_timeout_seconds, 1, 10),
            "timeout_overflow_mode": "throw",
            "max_result_rows": _bounded_int(self.max_raw_rows, 1, _MAX_RAW_ROWS),
            "max_result_bytes": _bounded_int(
                self.max_payload_bytes, 1024, _MAX_PAYLOAD_BYTES
            ),
            "result_overflow_mode": "throw",
        }

    def _check_client(self):
        """Pure metadata check, including ClickHouse Connect's silent LIMIT path.

        The real driver's validator legitimately omits a setting whose fixed
        server value already matches. Accept that case only with the explicit
        metadata proof; a dropped unknown/mismatched setting is not a budget.
        Recheck before each query because clients/settings are mutable.
        """
        client = self.client
        if (
            type(getattr(client, "query_limit", None)) is not int
            or client.query_limit != 0
        ):
            raise ValueError("CANDLE_READ_CLIENT_LIMIT_FORBIDDEN")
        if (
            type(getattr(client, "query_retries", None)) is not int
            or client.query_retries != 0
        ):
            raise ValueError("CANDLE_READ_CLIENT_RETRIES_FORBIDDEN")
        receive = getattr(client, "_send_receive_timeout", None)
        if type(receive) is not int or not self.query_timeout_seconds <= receive <= 15:
            raise ValueError("CANDLE_READ_CLIENT_TIMEOUT_UNVERIFIED")
        metadata = getattr(client, "server_settings", None)
        validate = getattr(client, "_validate_settings", None)
        current_setting = getattr(client, "get_client_setting", None)
        if (
            type(metadata) is not dict
            or not callable(validate)
            or not callable(current_setting)
        ):
            raise ValueError("CANDLE_READ_SETTINGS_UNVERIFIED")
        requested = self._settings()
        values = {}
        try:
            for key, value in requested.items():
                definition = metadata.get(key)
                actual = getattr(definition, "value", None)
                readonly = getattr(definition, "readonly", None)
                if (
                    type(actual) is not str
                    or type(readonly) is not int
                    or readonly not in (0, 1)
                ):
                    raise ValueError
                values[key] = actual
                # Fixed-profile mode only: even a supposedly writable setting
                # is not changed here. This also avoids ambiguity between a
                # per-setting constraint and the account's readonly mode.
                if actual != str(value):
                    raise ValueError
                configured = current_setting(key)
                if configured is not None and (
                    type(configured) not in (str, int) or str(configured) != str(value)
                ):
                    raise ValueError
            # Do not borrow a writable/default account and merely request
            # readonly=1 for this query. Least privilege is a composition gate.
            if values["readonly"] != "1":
                raise ValueError
            checked = validate(dict(requested))
            if type(checked) is not dict or not set(checked) <= set(requested):
                raise ValueError
            for key, wanted in requested.items():
                if key in checked:
                    if type(checked[key]) not in (str, int) or str(checked[key]) != str(
                        wanted
                    ):
                        raise ValueError
                elif values[key] != str(wanted):
                    raise ValueError
        except Exception:  # noqa: BLE001 - client/profile internals may contain connection details
            raise ValueError("CANDLE_READ_SETTINGS_UNVERIFIED") from None
        return requested

    def read(self, symbol, *, closed_at_ms, minutes=1440):
        settings = self._check_client()
        validate_symbol(symbol)
        milliseconds(closed_at_ms)
        _bounded_int(minutes, 1, _MAX_MINUTES)
        started = milliseconds(self.clock_ms())
        start = closed_at_ms - minutes * MINUTE
        if (
            closed_at_ms % MINUTE
            or start < 0
            or closed_at_ms > started
            or minutes > self.max_raw_rows
        ):
            raise ValueError("CANDLE_ARCHIVE_SCOPE")
        try:
            rows = self.client.query(
                "SELECT environment,symbol,open_ms,payload,content_digest "
                "FROM v2_market_candles_incremental "
                "WHERE environment={environment:String} AND symbol={symbol:String} "
                "AND open_ms>={start:UInt64} AND open_ms<{end:UInt64}",
                parameters={
                    "environment": "SANDBOX",
                    "symbol": symbol,
                    "start": start,
                    "end": closed_at_ms,
                },
                settings=settings,
            ).result_rows
        except Exception:  # noqa: BLE001 - sanitize external client diagnostics
            # Do not expose server addresses, SQL diagnostics or client secrets.
            raise ValueError("CANDLE_ARCHIVE_READ_FAILED") from None
        available = milliseconds(self.clock_ms())
        if available < started:
            raise ValueError("CANDLE_ARCHIVE_CLOCK_REGRESSION")
        if type(rows) not in (list, tuple) or len(rows) > self.max_raw_rows:
            raise ValueError("CANDLE_ARCHIVE_ROW_BOUND")
        found = {}
        payload_bytes = 0
        for row in rows:
            if type(row) not in (list, tuple) or len(row) != 5:
                raise ValueError("CANDLE_ARCHIVE_ROW_INVALID")
            environment, target, opened, payload, fingerprint = row
            if (
                environment != "SANDBOX"
                or target != symbol
                or type(opened) is not int
                or not start <= opened < closed_at_ms
                or opened % MINUTE
            ):
                raise ValueError("CANDLE_ARCHIVE_SCOPE")
            if type(payload) is not str:
                raise ValueError("CANDLE_ARCHIVE_PAYLOAD_INVALID")
            size = len(payload.encode())
            payload_bytes += size  # Raw duplicate payloads also consume budget.
            if size > _MAX_ROW_BYTES or payload_bytes > self.max_payload_bytes:
                raise ValueError("CANDLE_ARCHIVE_PAYLOAD_BOUND")
            if type(fingerprint) is bytes:
                try:
                    fingerprint = fingerprint.decode("ascii")
                except UnicodeError:
                    raise ValueError("CANDLE_ARCHIVE_CONFLICT") from None
            if type(fingerprint) is not str or digest(payload) != fingerprint:
                raise ValueError("CANDLE_ARCHIVE_CONFLICT")
            if opened in found:
                if found[opened][0] != payload:
                    raise ValueError("CANDLE_ARCHIVE_CONFLICT")
                continue
            bar = _json(payload)
            if (
                type(bar) is not dict
                or type(bar.get("t")) is not int
                or bar["t"] != opened
            ):
                raise ValueError("CANDLE_ARCHIVE_SCOPE")
            found[opened] = (payload, bar)
        if len(found) != minutes:
            raise ValueError("CANDLE_HISTORY_INCOMPLETE")
        bars = [found[opened][1] for opened in sorted(found)]
        return ClosedMarketBars(
            symbol=symbol,
            environment="SANDBOX",
            closed_at_ms=closed_at_ms,
            available_at_ms=available,
            bars_json=_encode_bars(bars),
        )
