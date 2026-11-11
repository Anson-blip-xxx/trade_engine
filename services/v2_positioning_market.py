"""Bounded credential-free LIVE public research collector; never order authority.

A budget is supplied by the caller. A collector instance shares its request and
wall-clock budget across symbols. Ordinary component failures do not erase the
others; quota, bans and deadlines latch a stop for the rest of the instance.
"""

import json
import time
from http.client import HTTPSConnection
from urllib.parse import urlencode

from v2_core.evidence import canonical, digest
from v2_core.positioning_evidence import ENDPOINTS, HOUR, SOURCE, STEP, VERSION, symbol

HOST = "fapi.binance.com"
CAP = 256 * 1024
TIMEOUT = 5


class PositioningMarketError(Exception):
    """Fixed-code public collector failure; never contains response bodies."""


class _Failure(PositioningMarketError):
    pass


def _timestamp(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("POSITIONING_TIMESTAMP_INVALID")
    return value


def _reject_constant(_):
    raise ValueError("POSITIONING_NONFINITE_JSON")


class PositioningMarket:
    def __init__(
        self,
        budget,
        *,
        factory=HTTPSConnection,
        clock_ms=lambda: time.time_ns() // 1_000_000,
        monotonic=time.monotonic,
        sleep=time.sleep,
        request_spacing=0.6,
        max_seconds=90,
        max_requests=64,
    ):
        if (
            type(request_spacing) not in (int, float)
            or not 0.5 <= request_spacing <= 5
            or type(max_seconds) is not int
            or not 30 <= max_seconds <= 180
            or type(max_requests) is not int
            or not 1 <= max_requests <= 100
        ):
            raise ValueError("POSITIONING_LIMIT_INVALID")
        self.budget, self.factory, self.clock = budget, factory, clock_ms
        self.monotonic, self.sleep, self.spacing = monotonic, sleep, request_spacing
        self.deadline = monotonic() + max_seconds
        self.next_at, self.requests, self.max_requests = 0, 0, max_requests
        self.halt_reason = None

    def _halt(self, reason):
        self.halt_reason = reason
        raise _Failure(reason)

    def _active(self, reserve=0):
        if self.halt_reason:
            raise _Failure(self.halt_reason)
        if self.monotonic() + reserve > self.deadline:
            self._halt("POSITIONING_DEADLINE")

    @staticmethod
    def _params(name, target, now):
        if name not in ENDPOINTS:
            raise ValueError("POSITIONING_ENDPOINT_DISABLED")
        if name == "premium_index":
            return {"symbol": target}
        if name == "hourly_klines":
            end = now // HOUR * HOUR
            return {
                "symbol": target,
                "interval": "1h",
                "startTime": end - 72 * HOUR,
                "endTime": end - 1,
                "limit": 72,
            }
        if name == "funding_history":
            return {
                "symbol": target,
                "startTime": now - 24 * HOUR,
                "endTime": now,
                "limit": 100,
            }
        end = now // STEP * STEP
        return {
            "symbol": target,
            "period": "5m",
            # A statistic may arrive one or two publication slots late. Keep
            # enough prior history for six hours ending at its actual event.
            # Inclusive coverage is at most 75 points, still below limit 80.
            "startTime": end - 6 * HOUR - 2 * STEP,
            "endTime": end,
            "limit": 80,
        }

    def _get(self, name, target, now):
        # The only URL construction path is this fixed allowlist. No arbitrary
        # host, headers, credentials, redirects, retries or exchange catalog.
        params = self._params(name, target, now)
        self._active()
        if self.requests >= self.max_requests:
            self._halt("POSITIONING_REQUEST_LIMIT")
        delay = max(0, self.next_at - self.monotonic())
        self._active(delay + TIMEOUT)
        if delay:
            self.sleep(delay)
        self._active(TIMEOUT)
        try:
            allowed = self.budget.permit(1)
        except Exception:  # noqa: BLE001 - injected quota failures halt, never bypass
            self._halt("POSITIONING_QUOTA_FAILURE")
        if not allowed:
            self._halt("POSITIONING_QUOTA_WAIT")
        self._active(TIMEOUT)
        self.requests += 1
        self.next_at = self.monotonic() + self.spacing
        connection = None
        try:
            connection = self.factory(HOST, timeout=TIMEOUT)
            connection.request("GET", ENDPOINTS[name] + "?" + urlencode(params))
            response = connection.getresponse()
            if response.status in (418, 429):
                try:
                    retry = response.getheader("Retry-After", "60")
                    seconds = (
                        min(259200, max(60, int(retry)))
                        if (
                            isinstance(retry, str)
                            and len(retry) <= 8
                            and retry.isdecimal()
                        )
                        else 60
                    )
                except Exception:  # noqa: BLE001 - untrusted Retry-After is optional
                    seconds = 60
                try:
                    self.budget.penalize(seconds)
                except Exception:  # noqa: BLE001, S110 - local ban remains latched
                    pass  # A failed shared penalty never clears the local halt.
                self._halt("POSITIONING_RATE_LIMITED")
            if response.status != 200:
                raise _Failure("POSITIONING_PUBLIC_HTTP_ERROR")
            raw = response.read(CAP + 1)
            self._active()
            if len(raw) > CAP:
                raise _Failure("POSITIONING_RESPONSE_TOO_LARGE")
            value = json.loads(
                raw,
                parse_float=str,
                parse_constant=_reject_constant,
            )
            if isinstance(value, dict) and "code" in value:
                raise _Failure("POSITIONING_PUBLIC_API_ERROR")
            if name == "premium_index":
                valid = isinstance(value, dict) and value.get("symbol") == target
            else:
                limit = (
                    72
                    if name == "hourly_klines"
                    else 100
                    if name == "funding_history"
                    else 80
                )
                valid = isinstance(value, list) and 0 < len(value) <= limit
            if not valid:
                raise _Failure("POSITIONING_RESPONSE_SHAPE")
            if name == "premium_index":
                value = [value]
            # Numeric API decimals remain strings. Actual field/range and time
            # validation is performed independently by describe().
            canonical({"rows": value})
            return value
        except _Failure:
            raise
        except Exception:  # noqa: BLE001 - sanitized independent component failure
            self._active()
            raise _Failure("POSITIONING_PUBLIC_TRANSPORT_FAILURE") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001, S110 - cleanup cannot mask evidence
                    pass

    def collect(self, target, *, now_ms):
        target, now = symbol(target), _timestamp(now_ms)
        started = _timestamp(self.clock())
        if now < 72 * HOUR or not now <= started <= now + 180_000:
            raise ValueError("POSITIONING_CURRENT_CLOCK_REQUIRED")
        components, previous = {}, started
        for name, endpoint in ENDPOINTS.items():
            try:
                rows, status, reason = self._get(name, target, now), "OBSERVED", None
            except _Failure as exc:
                rows, status, reason = [], "UNKNOWN", str(exc)
            fetched = _timestamp(self.clock())
            if fetched < previous:
                raise ValueError("POSITIONING_CLOCK_REGRESSED")
            previous = fetched
            components[name] = {
                "source": SOURCE,
                "source_host": HOST,
                "endpoint": endpoint,
                "status": status,
                "fetched_at_ms": fetched,
                "rows": rows,
                "reason": reason,
                "sha256": digest(canonical({"rows": rows})),
            }
        finished = _timestamp(self.clock())
        if finished < previous:
            raise ValueError("POSITIONING_CLOCK_REGRESSED")
        return {
            "version": VERSION,
            "source": SOURCE,
            "source_host": HOST,
            "symbol": target,
            "started_at_ms": started,
            "finished_at_ms": finished,
            "components": components,
            "execution_authorized": False,
            "automatic_parameter_updates": False,
            "promotion_authorized": False,
            "halt_reason": self.halt_reason,
        }
