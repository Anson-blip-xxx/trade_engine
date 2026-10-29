import json
from urllib.parse import parse_qs, urlsplit

import pytest

from services.v2_daily_mover_market import MoverMarket, MoverMarketError, public_symbol
from v2_core.daily_movers import DAY, MINUTE, analyze_intraday_path

START = 1792857600000  # 2026-10-25 00:00 UTC+8
END = START + DAY


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


class Budget:
    def __init__(self, *, allow=True):
        self.allow = allow
        self.permits = []
        self.penalties = []

    def permit(self, weight):
        self.permits.append(weight)
        return self.allow

    def penalize(self, seconds):
        self.penalties.append(seconds)


class Response:
    def __init__(self, value=None, *, status=200, raw=None, retry="60"):
        self.status, self.retry = status, retry
        self.raw = json.dumps(value).encode() if raw is None else raw
        self.read_limits = []

    def getheader(self, name, default=None):
        assert name == "Retry-After"
        return self.retry if self.retry is not None else default

    def read(self, limit):
        self.read_limits.append(limit)
        return self.raw[:limit]


class Connection:
    def __init__(self, harness):
        self.harness = harness
        self.url = None
        self.closed = False

    def request(self, method, url):
        assert method == "GET"
        self.url = url
        self.harness.calls.append((method, url))
        if self.harness.request_error:
            raise RuntimeError("sensitive request detail")

    def getresponse(self):
        if self.harness.responses:
            return self.harness.responses.pop(0)
        query = parse_qs(urlsplit(self.url).query)
        assert urlsplit(self.url).path == "/fapi/v1/klines"
        start, end = int(query["startTime"][0]), int(query["endTime"][0])
        step = MINUTE if query["interval"] == ["1m"] else 60 * MINUTE
        limit = int(query["limit"][0])
        rows = [
            [stamp, "100", "110", "90", "105", "1", stamp + step - 1]
            for stamp in range(start, end + 1, step)
        ]
        assert len(rows) <= limit
        return Response(rows)

    def close(self):
        self.closed = True
        if self.harness.close_error:
            raise RuntimeError("sensitive close detail")


class Harness:
    def __init__(self, *responses, allow=True, spacing=0.6, seconds=900):
        self.clock, self.budget = Clock(), Budget(allow=allow)
        self.responses, self.calls, self.connections = list(responses), [], []
        self.request_error = self.close_error = self.factory_error = False
        self.market = MoverMarket(
            self.budget,
            request_spacing=spacing,
            max_seconds=seconds,
            factory=self.factory,
            monotonic=self.clock.time,
            sleep=self.clock.sleep,
        )

    def factory(self, host, *, timeout):
        assert host == "fapi.binance.com" and timeout == 5
        if self.factory_error:
            raise RuntimeError("sensitive factory detail")
        connection = Connection(self)
        self.connections.append(connection)
        return connection


def params(**changes):
    return {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "startTime": START,
        "endTime": END - 1,
        "limit": 24,
        **changes,
    }


def symbol_row(symbol="BTCUSDT", **changes):
    return {
        "symbol": symbol,
        "quoteAsset": "USDT",
        "contractType": "PERPETUAL",
        "onboardDate": START - DAY,
        "deliveryDate": END + DAY,
        "status": "TRADING",
        **changes,
    }


@pytest.mark.parametrize(
    "path,query",
    [
        ("/fapi/v1/order", {}),
        ("/fapi/v2/positionRisk", {}),
        ("https://evil.example/fapi/v1/time", {}),
        ("/fapi/v1/time", {"signature": "secret"}),
        ("/fapi/v1/exchangeInfo", {"apiKey": "secret"}),
        ("/fapi/v1/klines", {**params(), "signature": "secret"}),
    ],
)
def test_only_allowlisted_public_gets_are_possible(path, query):
    harness = Harness()
    with pytest.raises(ValueError, match="MOVER_PUBLIC_ENDPOINT_DISABLED"):
        harness.market.get(path, query)
    assert not harness.calls and not harness.budget.permits


@pytest.mark.parametrize(
    "changes",
    [
        {"interval": "1d"},
        {"limit": 481},
        {"limit": 0},
        {"limit": True},
        {"startTime": True},
        {"endTime": float(END)},
        {"startTime": END},
        {"endTime": START + DAY + 1},
        {"symbol": "BTCUSDT&signature=secret"},
    ],
)
def test_malformed_kline_request_does_not_spend_quota(changes):
    harness = Harness()
    with pytest.raises(ValueError):
        harness.market.get("/fapi/v1/klines", params(**changes))
    assert not harness.calls and not harness.budget.permits


def test_complete_civil_day_hourly_summary_uses_24_rows_and_one_weight():
    harness = Harness()
    summary, rows = harness.market.summary(
        "BTCUSDT", start_ms=START, end_ms=END, as_of_ms=END
    )
    assert summary["complete"] is True and len(rows) == 24
    assert summary["source_interval_minutes"] == 60
    assert rows[0]["t"] == START and rows[-1]["t"] == END - 60 * MINUTE
    assert harness.budget.permits == [1]
    assert all(connection.closed for connection in harness.connections)


def test_top_mover_minute_path_fetches_three_disjoint_pages_of_480():
    harness = Harness()
    rows = harness.market.bars("BTCUSDT", START, END, interval_minutes=1)
    assert len(rows) == 1440 and len({row["t"] for row in rows}) == 1440
    assert [
        int(parse_qs(urlsplit(url).query)["limit"][0]) for _, url in harness.calls
    ] == [480] * 3
    assert rows[0]["t"] == START and rows[-1]["t"] == END - MINUTE
    assert harness.budget.permits == [2, 2, 2]
    assert harness.market.requests == 3 and harness.market.weight == 6
    assert harness.clock.sleeps == [1.2, 1.2]


def test_unicode_public_symbol_urlencoded_without_arbitrary_host_or_query():
    harness = Harness()
    harness.market.bars("币安人生USDT", START, END)
    _, url = harness.calls[0]
    assert "币安人生" not in url
    query = parse_qs(urlsplit(url).query)
    assert query["symbol"] == ["币安人生USDT"]
    assert len(query) == 5


@pytest.mark.parametrize(
    "symbol", ["../BTCUSDT", "BTC?USDT", "BTC\nUSDT", " BTCUSDT", "币" * 41 + "USDT"]
)
def test_public_symbol_rejects_unsafe_and_core_incompatible_lengths(symbol):
    with pytest.raises(ValueError, match="MOVER_PUBLIC_SYMBOL_INVALID"):
        public_symbol(symbol)


@pytest.mark.parametrize(
    "status,retry,penalty",
    [
        (429, "60", 60),
        (418, "300", 300),
        (429, "0", 60),
        (429, "9999999", 259200),
        (418, "nonsense", 60),
        (429, None, 60),
    ],
)
def test_venue_rate_limit_penalizes_shared_budget_without_retrying(
    status, retry, penalty
):
    harness = Harness(Response(status=status, retry=retry))
    with pytest.raises(MoverMarketError, match="MOVER_RATE_LIMITED"):
        harness.market.get("/fapi/v1/time", {})
    assert harness.budget.penalties == [penalty]
    assert harness.market.requests == 1 and len(harness.calls) == 1
    assert harness.connections[0].closed


def test_quota_denial_never_creates_connection_and_stops_page_loop():
    harness = Harness(allow=False)
    with pytest.raises(MoverMarketError, match="MOVER_SHARED_QUOTA_WAIT"):
        harness.market.bars("BTCUSDT", START, END, interval_minutes=1)
    assert harness.budget.permits == [2]
    assert not harness.connections and harness.market.requests == 0


def test_deadline_prevents_request_and_does_not_burn_shared_quota():
    harness = Harness(seconds=30)
    harness.clock.now = 26
    with pytest.raises(MoverMarketError, match="MOVER_JOB_DEADLINE"):
        harness.market.get("/fapi/v1/time", {})
    assert not harness.budget.permits and not harness.calls


def test_pacing_delay_is_included_in_deadline_budget():
    harness = Harness(Response({"serverTime": END}), seconds=30, spacing=5)
    harness.clock.now = 21
    harness.market.get("/fapi/v1/time", {})
    with pytest.raises(MoverMarketError, match="MOVER_JOB_DEADLINE"):
        harness.market.get("/fapi/v1/time", {})
    assert len(harness.calls) == 1
    assert harness.clock.sleeps == []


@pytest.mark.parametrize(
    "response,reason",
    [
        (Response(status=503), "MOVER_PUBLIC_HTTP_ERROR"),
        (Response(status=302), "MOVER_PUBLIC_HTTP_ERROR"),
        (Response(raw=b"not json"), "MOVER_PUBLIC_TRANSPORT_FAILURE"),
        (Response({"code": -1121, "msg": "Invalid symbol"}), "MOVER_PUBLIC_API_ERROR"),
        (Response(raw=b"x" * 4_000_001), "MOVER_RESPONSE_TOO_LARGE"),
    ],
)
def test_response_bounds_errors_and_redirects_fail_closed(response, reason):
    harness = Harness(response)
    with pytest.raises(MoverMarketError, match=reason):
        harness.market.get("/fapi/v1/time", {})
    assert harness.connections[0].closed
    assert harness.budget.permits == [1]
    if response.read_limits:
        assert response.read_limits == [4_000_001]


@pytest.mark.parametrize("point", ["request_error", "factory_error"])
def test_transport_errors_are_sanitized_and_attempt_quota_counted(point):
    harness = Harness()
    setattr(harness, point, True)
    with pytest.raises(MoverMarketError, match="^MOVER_PUBLIC_TRANSPORT_FAILURE$"):
        harness.market.get("/fapi/v1/time", {})
    assert harness.budget.permits == [1]


def test_close_error_must_not_mask_shared_rate_limit_signal():
    harness = Harness(Response(status=429))
    harness.close_error = True
    with pytest.raises(MoverMarketError, match="^MOVER_RATE_LIMITED$"):
        harness.market.get("/fapi/v1/time", {})
    assert harness.budget.penalties == [60]


def test_catalog_filters_quote_and_contract_not_unicode_or_current_status():
    harness = Harness(
        Response(
            {
                "symbols": [
                    symbol_row(),
                    symbol_row("币安人生USDT"),
                    symbol_row("OLDUSDT", status="SETTLING"),
                    symbol_row("BTCUSDC", quoteAsset="USDC"),
                    symbol_row("BTCUSDT_2612", contractType="CURRENT_QUARTER"),
                ]
            }
        )
    )
    assert [row["symbol"] for row in harness.market.catalog()] == [
        "BTCUSDT",
        "币安人生USDT",
        "OLDUSDT",
    ]


@pytest.mark.parametrize(
    "value,reason",
    [
        ([], "MOVER_CATALOG_INVALID"),
        ({"symbols": []}, "MOVER_CATALOG_BOUND"),
        ({"symbols": [symbol_row(), symbol_row()]}, "MOVER_CATALOG_DUPLICATE_OR_EMPTY"),
        ({"symbols": ["malformed"]}, "MOVER_CATALOG_INVALID"),
    ],
)
def test_bad_catalog_is_bounded_and_sanitized(value, reason):
    harness = Harness(Response(value))
    with pytest.raises(MoverMarketError, match=reason):
        harness.market.catalog()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        [[START, "100"]],
        [[START - MINUTE, "100", "100", "100", "100", "1", START - 1]],
        [[START, "100", "100", "100", "100", "1", START + MINUTE]],
        [[True, "100", "100", "100", "100", "1", START + MINUTE - 1]],
    ],
)
def test_malformed_kline_envelope_never_creates_daily_summary(bad):
    harness = Harness(Response(bad))
    with pytest.raises(MoverMarketError, match="MOVER_KLINES_INVALID"):
        harness.market.bars("BTCUSDT", START, END)


@pytest.mark.parametrize("defect", ["gap", "duplicate", "float", "infinite", "ohlc"])
def test_invalid_source_is_unknown_not_fabricated_complete_summary(defect):
    step = 60 * MINUTE
    rows = [
        [stamp, "100", "110", "90", "105", "1", stamp + step - 1]
        for stamp in range(START, END, step)
    ]
    if defect == "gap":
        rows.pop()
    elif defect == "duplicate":
        rows[10] = rows[9]
    elif defect == "float":
        rows[10][4] = 100.0
    elif defect == "infinite":
        rows[10][4] = "Infinity"
    else:
        rows[10][2] = "1"
    harness = Harness(Response(rows))
    summary, _ = harness.market.summary(
        "BTCUSDT", start_ms=START, end_ms=END, as_of_ms=END
    )
    assert summary["complete"] is False and summary["status"] == "UNKNOWN"


def test_empty_minute_response_is_unknown_not_synthetic_flat_market():
    harness = Harness(Response([]), Response([]), Response([]))
    rows = harness.market.bars("BTCUSDT", START, END, interval_minutes=1)
    result = analyze_intraday_path(rows, start_ms=START, end_ms=END, as_of_ms=END)
    assert result["status"] == "UNKNOWN" and result["features"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"interval_minutes": True},
        {"start_ms": START + 1, "end_ms": END + 1},
        {"start_ms": True, "end_ms": DAY + 1},
    ],
)
def test_invalid_source_window_rejected_before_network(changes):
    harness = Harness()
    options = {"start_ms": START, "end_ms": END, "interval_minutes": 60, **changes}
    with pytest.raises(ValueError):
        harness.market.bars("BTCUSDT", **options)
    assert not harness.calls


def test_unclosed_summary_does_not_fetch_future_intraday_rows():
    harness = Harness()
    with pytest.raises(ValueError, match="MOVER_DAY_NOT_CLOSED"):
        harness.market.summary("BTCUSDT", start_ms=START, end_ms=END, as_of_ms=END - 1)
    assert not harness.calls
