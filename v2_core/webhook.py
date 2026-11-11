"""V2 TradingView WSGI ingress. No listener, credentials discovery or trading.

Deploy behind authenticated TLS termination with finite body-read timeouts,
request/concurrency limits, and body logging disabled. Construction is explicit.
"""

import hmac
import json
from hashlib import sha256
from http import HTTPStatus

from v2_core.ingress import IntakeRejected, SignalIngress, milliseconds, symbol
from v2_core.ledger import amount
from v2_core.signals import SignalConflict

_SIGNALS = {
    "TREND_UP_LONG": "TREND_UP",
    "PULSE_UP_LONG": "PULSE_UP",
    "VIOLENT_LONG": "VIOLENT_BULLISH",
    "PUMP_LONG": "PUMP_UP",
    "TREND_DOWN_SHORT": "TREND_DOWN",
    "PULSE_DOWN_SHORT": "PULSE_DOWN",
    "VIOLENT_SHORT": "VIOLENT_BEARISH",
    "PANIC_SELL_SHORT": "PANIC_SELL",
    "PUMP_SHORT": "PUMP_DOWN",
}

_PRODUCER_VERSION = "tv-causal-15m-v2"
_PRODUCER_SETUPS = {
    "TREND_TRANSITION": {"TREND_UP_LONG", "TREND_DOWN_SHORT"},
    "TREND_BREAKOUT": {"TREND_UP_LONG", "TREND_DOWN_SHORT"},
    "TREND_PULLBACK_RESUME": {"TREND_UP_LONG", "TREND_DOWN_SHORT"},
    "PULSE": {"PULSE_UP_LONG", "PULSE_DOWN_SHORT"},
    "PUMP": {"PUMP_LONG", "PUMP_SHORT"},
    "VIOLENT": {"VIOLENT_LONG", "VIOLENT_SHORT"},
}


def _producer_evidence(value, *, observed_at, signal):
    """Allowlisted audit metadata only; it grants no entry/structural permission."""
    if not isinstance(value, dict) or set(value) != {
        "version", "setup", "timeframe", "bar_open_ms", "bar_close_ms"
    }:
        raise IntakeRejected("INVALID_PRODUCER_METADATA")
    setup = value["setup"]
    if (
        value["version"] != _PRODUCER_VERSION
        or value["timeframe"] != "15"
        or not isinstance(setup, str)
        or signal not in _PRODUCER_SETUPS.get(setup, ())
    ):
        raise IntakeRejected("INVALID_PRODUCER_METADATA")
    opened = milliseconds(value["bar_open_ms"])
    closed = milliseconds(value["bar_close_ms"])
    observed = milliseconds(observed_at)
    if opened % 900000 or closed != opened + 900000 or closed != observed:
        raise IntakeRejected("INVALID_PRODUCER_BAR_TIME")
    return dict(value)


class AuthenticationRejected(ValueError):
    """Only authentication failures map to HTTP 401."""


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IntakeRejected("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_number(_):
    raise IntakeRejected("DECIMAL_STRINGS_REQUIRED")


def _event_identity(payload, target):
    """Encode only a field-bound Unicode identity from the universal Pine alert.

    Generic event IDs keep the ingress ASCII contract. The symbol, bar clock and
    producer signal make the original Unicode ID reconstructible from evidence.
    """
    value = payload["event_id"]
    if isinstance(value, str) and value.isascii():
        return value
    ticker = f"BINANCE:{target}.P"
    observed = milliseconds(payload["observed_at"])
    expected = f"tv:{ticker}:15:{observed}:{payload['signal']}"
    if target.isascii() or payload["symbol"] != ticker or value != expected:
        raise IntakeRejected("INVALID_EVENT_ID")
    return "tv-u:" + sha256(value.encode("utf-8")).hexdigest()


class TradingViewWebhook:
    def __init__(self, ingress, *, secret):
        if not isinstance(ingress, SignalIngress) or ingress.source != "tv_bridge":
            raise ValueError("bound TradingView ingress required")
        if not isinstance(secret, str) or not 32 <= len(secret.encode()) <= 512:
            raise ValueError("explicit 32-512 byte webhook secret required")
        self.ingress, self._secret = ingress, secret.encode()

    def receive(self, body):
        if not isinstance(body, bytes) or not 0 < len(body) <= 16384:
            raise IntakeRejected("INVALID_BODY_SIZE")
        try:
            payload = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_unique_pairs,
                parse_float=_reject_number,
                parse_constant=_reject_number,
            )
            # JSON escape syntax can encode lone surrogates despite valid UTF-8
            # input. Reject those as input errors, not storage outages.
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (UnicodeError, ValueError, RecursionError):
            raise IntakeRejected("INVALID_JSON") from None
        if not isinstance(payload, dict):
            raise IntakeRejected("INVALID_JSON_OBJECT")
        supplied = payload.pop("secret", None)
        if not isinstance(supplied, str) or not hmac.compare_digest(
            supplied.encode(), self._secret
        ):
            raise AuthenticationRejected("AUTHENTICATION_FAILED")
        required = {
            "event_id",
            "observed_at",
            "expires_at_ms",
            "symbol",
            "signal",
            "price",
            "strength",
        }
        optional = {
            "taker_buy_ratio",
            "orderflow_bias",
            "chg_15m",
            "chg_1h",
            "vol_1h",
            "producer",
        }
        if not required <= payload.keys() or not payload.keys() <= required | optional:
            raise IntakeRejected("INVALID_ALERT_FIELDS")
        raw_symbol = payload["symbol"]
        if not isinstance(raw_symbol, str):
            raise IntakeRejected("INVALID_SYMBOL")
        # Retain the established ASCII alert convenience syntax. Unicode venue
        # identities must not be changed by case folding or whitespace removal.
        normalized = (
            raw_symbol.strip().upper() if raw_symbol.isascii() else raw_symbol
        )
        normalized = normalized.removeprefix("BINANCE:").removesuffix(".P")
        symbol(normalized)
        if not isinstance(payload["signal"], str) or payload["signal"] not in _SIGNALS:
            raise IntakeRejected("INVALID_SIGNAL")
        if type(payload["strength"]) is not int or not 0 <= payload["strength"] <= 100:
            raise IntakeRejected("INVALID_STRENGTH")
        features = {"tv_signal": payload["signal"], "strength": payload["strength"]}
        if "producer" in payload:
            features["tv_producer"] = _producer_evidence(
                payload["producer"],
                observed_at=payload["observed_at"],
                signal=payload["signal"],
            )
        try:
            features["price"] = format(amount(payload["price"], positive=True), "f")
            for key, low, high in (
                ("taker_buy_ratio", 0, 1),
                ("orderflow_bias", -1, 1),
                ("chg_15m", -100, 10000),
                ("chg_1h", -100, 10000),
                ("vol_1h", 0, 10000),
            ):
                if key in payload:
                    value = amount(payload[key])
                    if not low <= value <= high:
                        raise ValueError("range")
                    features[key] = format(value, "f")
        except ValueError:
            raise IntakeRejected("INVALID_MARKET_VALUE") from None
        return self.ingress.accept(
            {
                "event_id": _event_identity(payload, normalized),
                "observed_at": payload["observed_at"],
                "expires_at_ms": payload["expires_at_ms"],
                "symbol": normalized,
                "signal": _SIGNALS[payload["signal"]],
                "features": features,
            }
        )

    def __call__(self, environ, start_response):
        status, result = 200, None
        try:
            if environ.get("PATH_INFO") != "/v2/webhooks/tradingview":
                status, result = 404, {"error": "NOT_FOUND"}
            elif environ.get("REQUEST_METHOD") != "POST":
                status, result = 405, {"error": "METHOD_NOT_ALLOWED"}
            elif (
                environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                status, result = 415, {"error": "JSON_REQUIRED"}
            elif environ.get("HTTP_TRANSFER_ENCODING"):
                status, result = 400, {"error": "TRANSFER_ENCODING_UNSUPPORTED"}
            else:
                length = environ.get("CONTENT_LENGTH", "")
                if not length.isascii() or not length.isdigit() or len(length) > 5:
                    raise IntakeRejected("INVALID_CONTENT_LENGTH")
                size = int(length)
                if not 0 < size <= 16384:
                    status, result = 413, {"error": "BODY_TOO_LARGE"}
                else:
                    body = environ["wsgi.input"].read(size)
                    if len(body) != size:
                        raise IntakeRejected("TRUNCATED_BODY")
                    result = {"status": "RECORDED", "signal_id": self.receive(body)}
        except AuthenticationRejected:
            status, result = 401, {"error": "AUTHENTICATION_FAILED"}
        except SignalConflict:
            status, result = 409, {"error": "EVENT_ID_CONFLICT"}
        except IntakeRejected as exc:
            status, result = 400, {"error": str(exc)}
        except Exception:  # noqa: BLE001 - never expose DB/transport secrets or acknowledge failed commits
            status, result = 503, {"error": "INGRESS_UNAVAILABLE"}
        encoded = json.dumps(result, separators=(",", ":")).encode()
        headers = [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(encoded))),
            ("Cache-Control", "no-store"),
        ]
        if status == 405:
            headers.append(("Allow", "POST"))
        if status == 503:
            headers.append(("Retry-After", "5"))
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [encoded]
