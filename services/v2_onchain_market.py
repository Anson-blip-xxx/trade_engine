"""Bounded keyless DEX snapshots for research, never an execution dependency.

Only two GET paths on the fixed public host are supported. Token summaries are
provider aggregates, not verified swaps. Pool values cannot establish LP flows,
wallet ownership, chain finality, or coverage of the full market.
"""

import json
import math
import queue
import re
import socket
import threading
import time
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from http.client import HTTPSConnection
from urllib.parse import urlencode

from v2_core.evidence import canonical, digest

HOST = "api.dexpaprika.com"
VERSION = "onchain-public-observation-v1"
SOURCE = "DEXPAPRIKA_PUBLIC_KEYLESS"
CAP = 128 * 1024
TIMEOUT = 3
NETWORKS = frozenset(
    (
        "ethereum",
        "bsc",
        "base",
        "arbitrum",
        "optimism",
        "polygon",
        "avalanche",
        "solana",
    )
)
WINDOWS = ("1m", "5m", "15m", "30m", "1h", "6h", "24h")


class OnchainMarketError(Exception):
    """A fixed-code failure with no response body, credentials or remote text."""

    def __init__(self, code, retry_after_seconds=0, *, halt=False):
        super().__init__(code)
        self.retry_after_seconds = retry_after_seconds
        self.halt = halt


def _address(network, value):
    if not isinstance(network, str) or network not in NETWORKS:
        raise ValueError("ONCHAIN_NETWORK_UNSUPPORTED")
    if not isinstance(value, str):
        raise TypeError("ONCHAIN_ADDRESS_INVALID")
    if network == "solana":
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", value):
            raise ValueError("ONCHAIN_ADDRESS_INVALID")
        return value
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise ValueError("ONCHAIN_ADDRESS_INVALID")
    return value.lower()


def _timestamp(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("ONCHAIN_TIMESTAMP_INVALID")
    return value


def _reject_constant(_):
    raise ValueError("ONCHAIN_NONFINITE_JSON")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ONCHAIN_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _text(value, maximum=160):
    if not isinstance(value, str) or len(value) > maximum or not value.isprintable():
        raise ValueError("ONCHAIN_TEXT_INVALID")
    return value


def _decimal(value, *, signed=False):
    if type(value) not in (str, int) or len(str(value)) > 128:
        raise ValueError("ONCHAIN_NUMBER_INVALID")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("ONCHAIN_NUMBER_INVALID") from None
    if (
        not number.is_finite()
        or (not signed and number < 0)
        or number.copy_abs() > Decimal("1e100")
        or number.as_tuple().exponent < -100
    ):
        raise ValueError("ONCHAIN_NUMBER_INVALID")
    return str(number)


def _count(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("ONCHAIN_COUNT_INVALID")
    return value


def _fields(source, *, decimals=(), signed=(), counts=(), text=()):
    if not isinstance(source, dict):
        raise TypeError("ONCHAIN_OBJECT_INVALID")
    result = {}
    for key in decimals:
        if source.get(key) is not None:
            result[key] = _decimal(source[key])
    for key in signed:
        if source.get(key) is not None:
            result[key] = _decimal(source[key], signed=True)
    for key in counts:
        if source.get(key) is not None:
            result[key] = _count(source[key])
    for key in text:
        if source.get(key) is not None:
            result[key] = _text(source[key])
    return result


def _token(value, network, address):
    if (
        not isinstance(value, dict)
        or value.get("chain") != network
        or _address(network, value.get("id")) != address
        or not isinstance(value.get("summary"), dict)
    ):
        raise ValueError("ONCHAIN_TOKEN_IDENTITY_MISMATCH")
    result = {"id": address, "chain": network}
    result.update(_fields(value, text=("symbol", "name", "last_updated")))
    if "decimals" in value:
        decimals = _count(value["decimals"])
        if decimals > 255:
            raise ValueError("ONCHAIN_DECIMALS_INVALID")
        result["decimals"] = decimals
    if "no_index" in value:
        if type(value["no_index"]) is not bool:
            raise ValueError("ONCHAIN_INDEX_FLAG_INVALID")
        result["no_index"] = value["no_index"]
    summary = value["summary"]
    if "chain" in summary and summary["chain"] != network:
        raise ValueError("ONCHAIN_SUMMARY_IDENTITY_MISMATCH")
    if "id" in summary and _address(network, summary["id"]) != address:
        raise ValueError("ONCHAIN_SUMMARY_IDENTITY_MISMATCH")
    clean = _fields(
        summary, decimals=("price_usd", "fdv", "liquidity_usd"), counts=("pools",)
    )
    if "id" in summary:
        clean["id"] = address
    if "chain" in summary:
        clean["chain"] = network
    for window in WINDOWS:
        if summary.get(window) is not None:
            clean[window] = _fields(
                summary[window],
                decimals=("volume", "volume_usd", "buy_usd", "sell_usd"),
                signed=("last_price_usd_change",),
                counts=("buys", "sells", "txns"),
            )
    result["summary"] = clean
    return result


def _pools(value, network, address):
    if not isinstance(value, dict) or not isinstance(value.get("query"), dict):
        raise TypeError("ONCHAIN_POOL_QUERY_MISSING")
    query = value["query"]
    if (
        query.get("network") != network
        or _address(network, query.get("token_address")) != address
        or type(query.get("limit")) is not int
        or query["limit"] != 3
        or query.get("order_by") != "volume_usd_24h"
        or query.get("sort") != "desc"
    ):
        raise ValueError("ONCHAIN_POOL_QUERY_MISMATCH")
    rows = value.get("results")
    if (
        not isinstance(rows, list)
        or len(rows) > 3
        or type(value.get("has_next_page")) is not bool
    ):
        raise ValueError("ONCHAIN_POOL_ROWS_INVALID")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get("chain") != network:
            raise ValueError("ONCHAIN_POOL_IDENTITY_MISMATCH")
        pool_id = _text(row.get("id"))
        if not re.fullmatch(r"[a-zA-Z0-9:_-]{20,160}", pool_id):
            raise ValueError("ONCHAIN_POOL_ID_INVALID")
        if network != "solana":
            pool_id = pool_id.lower()
        if pool_id in seen:
            raise ValueError("ONCHAIN_DUPLICATE_POOL")
        seen.add(pool_id)
        tokens = row.get("tokens")
        if not isinstance(tokens, list) or not 2 <= len(tokens) <= 8:
            raise ValueError("ONCHAIN_POOL_TOKENS_INVALID")
        token_ids = []
        for token in tokens:
            if not isinstance(token, dict) or token.get("chain") != network:
                raise ValueError("ONCHAIN_POOL_TOKEN_IDENTITY_MISMATCH")
            token_ids.append(_address(network, token.get("id")))
        if address not in token_ids or len(set(token_ids)) != len(token_ids):
            raise ValueError("ONCHAIN_POOL_TOKEN_IDENTITY_MISMATCH")
        clean = {
            "id": pool_id,
            "chain": network,
            "tokens": [{"id": item, "chain": network} for item in token_ids],
        }
        clean.update(
            _fields(
                row,
                decimals=("volume_usd_24h", "liquidity_usd", "price_usd"),
                signed=tuple(
                    f"price_change_percentage_{window}"
                    for window in ("5m", "1h", "6h", "24h")
                ),
                counts=("transactions_24h",),
                text=("dex_id", "dex_name", "created_at"),
            )
        )
        result.append(clean)
    return {
        "results": result,
        "has_next_page": value["has_next_page"],
        "query": {
            "network": network,
            "token_address": address,
            "limit": 3,
            "order_by": "volume_usd_24h",
            "sort": "desc",
        },
    }


class OnchainMarket:
    """One bounded batch; a failure is evidence UNKNOWN, never a trading veto."""

    def __init__(
        self,
        *,
        factory=HTTPSConnection,
        clock_ms=lambda: time.time_ns() // 1_000_000,
        monotonic=time.monotonic,
        sleep=time.sleep,
        max_seconds=50,
        max_requests=6,
        request_spacing=6,
    ):
        if (
            type(max_seconds) is not int
            or not 3 <= max_seconds <= 50
            or type(max_requests) is not int
            or not 1 <= max_requests <= 6
            or type(request_spacing) not in (int, float)
            or not math.isfinite(request_spacing)
            or not 6 <= request_spacing <= 30
        ):
            raise ValueError("ONCHAIN_LIMIT_INVALID")
        self.factory, self.clock = factory, clock_ms
        self.monotonic, self.sleep = monotonic, sleep
        self.spacing, self.max_requests = request_spacing, max_requests
        self.deadline = monotonic() + max_seconds
        self.next_at, self.requests = 0, 0
        self.halt_reason, self.retry_after_seconds = None, 0

    def _halt(self, reason, retry=0):
        self.halt_reason, self.retry_after_seconds = reason, retry
        raise OnchainMarketError(reason, retry, halt=True)

    def _active(self, reserve=0):
        if self.halt_reason:
            raise OnchainMarketError(
                self.halt_reason, self.retry_after_seconds, halt=True
            )
        if self.monotonic() + reserve > self.deadline:
            self._halt("ONCHAIN_DEADLINE")

    def _retry_after(self, response, default):
        try:
            raw = response.getheader("Retry-After", None)
            if not isinstance(raw, str) or len(raw) > 128:
                return default
            if raw.isdecimal() and len(raw) <= 10:
                seconds = int(raw)
            else:
                stamp = parsedate_to_datetime(raw)
                if stamp.tzinfo is None:
                    return default
                seconds = math.ceil(stamp.timestamp() - self.clock() / 1000)
            return min(86400, max(6, seconds))
        except Exception:  # noqa: BLE001 - optional untrusted header
            return default

    def _request(self, path):
        # A daemon worker caps DNS, response-header and slow-drip hangs too,
        # unlike socket read timeouts alone. A timeout halts this entire batch;
        # no second worker or retry is launched while a timed-out GET may live.
        outcome, cancelled, connections = queue.Queue(maxsize=1), threading.Event(), []

        def fetch():
            connection = None
            try:
                if cancelled.is_set():
                    return
                connection = self.factory(HOST, timeout=TIMEOUT)
                connections.append(connection)
                if cancelled.is_set():
                    return
                connection.request("GET", path, headers={"Accept": "application/json"})
                response = connection.getresponse()
                status = response.status
                if status in (414, 418, 429):
                    raise OnchainMarketError(
                        "ONCHAIN_RATE_LIMITED",
                        self._retry_after(response, 60),
                        halt=True,
                    )
                if status in (401, 402, 403):
                    code = (
                        "ONCHAIN_CREDIT_EXHAUSTED"
                        if status == 402
                        else "ONCHAIN_ACCESS_DENIED"
                    )
                    raise OnchainMarketError(
                        code, self._retry_after(response, 3600), halt=True
                    )
                if 300 <= status < 400:
                    raise OnchainMarketError("ONCHAIN_REDIRECT_BLOCKED", 300, halt=True)
                if 500 <= status < 600:
                    raise OnchainMarketError(
                        "ONCHAIN_PROVIDER_UNAVAILABLE",
                        self._retry_after(response, 60),
                        halt=True,
                    )
                if status != 200:
                    raise OnchainMarketError("ONCHAIN_PUBLIC_HTTP_ERROR")
                length = response.getheader("Content-Length", None)
                if isinstance(length, str) and length.isdecimal() and int(length) > CAP:
                    raise OnchainMarketError("ONCHAIN_RESPONSE_TOO_LARGE")
                raw = response.read(CAP + 1)
                if len(raw) > CAP:
                    raise OnchainMarketError("ONCHAIN_RESPONSE_TOO_LARGE")
                try:
                    result = json.loads(
                        raw,
                        parse_float=str,
                        parse_constant=_reject_constant,
                        object_pairs_hook=_object,
                    )
                except (ValueError, UnicodeError, RecursionError):
                    raise OnchainMarketError("ONCHAIN_INVALID_JSON") from None
                outcome.put_nowait(result)
            except OnchainMarketError as exc:
                outcome.put_nowait(exc)
            except TimeoutError:
                outcome.put_nowait(
                    OnchainMarketError("ONCHAIN_REQUEST_TIMEOUT", 60, halt=True)
                )
            except Exception:  # noqa: BLE001 - hide transport and payload details
                outcome.put_nowait(
                    OnchainMarketError("ONCHAIN_TRANSPORT_FAILURE", 60, halt=True)
                )
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:  # noqa: BLE001, S110 - cleanup is best effort
                        pass

        try:
            threading.Thread(
                target=fetch, daemon=True, name="onchain-public-get"
            ).start()
        except RuntimeError:
            self._halt("ONCHAIN_TRANSPORT_FAILURE", 60)
        try:
            result = outcome.get(
                timeout=min(TIMEOUT, max(0, self.deadline - self.monotonic()))
            )
        except queue.Empty:
            cancelled.set()
            for connection in connections:
                try:
                    if connection.sock is not None:
                        connection.sock.shutdown(socket.SHUT_RDWR)
                except Exception:  # noqa: BLE001, S110 - not all failed sockets can shut down
                    pass
            self._halt("ONCHAIN_REQUEST_TIMEOUT", 60)
        self._active()
        if isinstance(result, OnchainMarketError):
            if result.halt:
                self._halt(str(result), result.retry_after_seconds)
            raise result
        return result

    def _get(self, name, network, address):
        self._active()
        if self.requests >= self.max_requests:
            self._halt("ONCHAIN_REQUEST_LIMIT")
        delay = max(0, self.next_at - self.monotonic())
        self._active(delay + TIMEOUT)
        if delay:
            self.sleep(delay)
        self._active(TIMEOUT)
        self.requests += 1
        self.next_at = self.monotonic() + self.spacing
        if name == "token":
            path = f"/networks/{network}/tokens/{address}"
        elif name == "pools":
            path = f"/networks/{network}/pools/search?" + urlencode(
                {
                    "token_address": address,
                    "order_by": "volume_usd_24h",
                    "sort": "desc",
                    "limit": 3,
                }
            )
        else:
            raise ValueError("ONCHAIN_ENDPOINT_DISABLED")
        value = self._request(path)
        try:
            return (_token if name == "token" else _pools)(value, network, address)
        except (ValueError, TypeError, KeyError):
            raise OnchainMarketError("ONCHAIN_RESPONSE_SHAPE") from None

    def collect(self, network_id, token_address, *, now_ms):
        address, now = _address(network_id, token_address), _timestamp(now_ms)
        started = _timestamp(self.clock())
        if not now <= started <= now + 50_000:
            raise ValueError("ONCHAIN_CURRENT_CLOCK_REQUIRED")
        components, previous = {}, started
        for name in ("token", "pools"):
            try:
                payload = self._get(name, network_id, address)
                status, reason, retry = "OBSERVED", None, 0
            except OnchainMarketError as exc:
                payload, status, reason = None, "UNKNOWN", str(exc)
                retry = exc.retry_after_seconds
            fetched = _timestamp(self.clock())
            if fetched < previous:
                raise ValueError("ONCHAIN_CLOCK_REGRESSED")
            previous = fetched
            endpoint = (
                f"/networks/{network_id}/tokens/{address}"
                if name == "token"
                else f"/networks/{network_id}/pools/search"
            )
            components[name] = {
                "status": status,
                "endpoint": endpoint,
                "fetched_at_ms": fetched,
                "payload": payload,
                "reason": reason,
                "retry_after_seconds": retry,
                "sha256": digest(canonical({"payload": payload})),
            }
        return {
            "version": VERSION,
            "source": SOURCE,
            "provider": "DEXPAPRIKA",
            "network_id": network_id,
            "token_address": address,
            "started_at_ms": started,
            "finished_at_ms": previous,
            "components": components,
            "halt_reason": self.halt_reason,
            "retry_after_seconds": self.retry_after_seconds,
            "execution_authorized": False,
            "automatic_parameter_updates": False,
            "promotion_authorized": False,
        }
