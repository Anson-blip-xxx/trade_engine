import json
from urllib.parse import parse_qs, urlsplit

import pytest

from services.v2_daily_mover_market import MoverMarketError
from services.v2_mover_flow_market import FlowMarket
from v2_core.daily_movers import DAY

START = 1792857600000  # 2026-10-25 00:00 UTC+8
END = START + DAY
HOUR = 3_600_000
FUTURES_CATALOG = "/fapi/v1/exchangeInfo"
SPOT_CATALOG = "/api/v3/exchangeInfo"
SPOT = "/api/v3/klines"
OI = "/futures/data/openInterestHist"
FUNDING = "/fapi/v1/fundingRate"


def symbol_row(symbol="BTCUSDT", base="BTC", **changes):
    return {
        "symbol": symbol,
        "baseAsset": base,
        "quoteAsset": "USDT",
        "contractType": "PERPETUAL",
        **changes,
    }


def spot_rows():
    return [
        [
            stamp,
            "100",
            "110",
            "90",
            "105",
            "1",
            stamp + HOUR - 1,
            "105",
            10,
            "0.6",
            "63",
            "0",
        ]
        for stamp in range(START, END, HOUR)
    ]


def oi_rows(symbol="BTCUSDT"):
    return [
        {
            "symbol": symbol,
            "timestamp": stamp,
            "sumOpenInterest": "2",
            "sumOpenInterestValue": "210",
            "CMCCirculatingSupply": "1000",
        }
        for stamp in range(START, END + 1, HOUR)
    ]


def funding_rows(symbol="BTCUSDT"):
    return [
        {
            "symbol": symbol,
            "fundingTime": stamp,
            "fundingRate": "0.0001",
            "markPrice": "105",
            "rateType": "Regular",
        }
        for stamp in range(START - DAY, END + 1, 8 * HOUR)
    ]


def params(path=SPOT, **changes):
    value = {
        "symbol": "BTCUSDT",
        "startTime": START,
        "endTime": END - 1,
        "interval": "1h",
        "limit": 24,
    }
    if path == OI:
        value = {
            "symbol": "BTCUSDT",
            "startTime": START,
            "endTime": END,
            "period": "1h",
            "limit": 30,
        }
    elif path == FUNDING:
        value = {
            "symbol": "BTCUSDT",
            "startTime": START - DAY,
            "endTime": END,
            "limit": 200,
        }
    return {**value, **changes}


class Response:
    def __init__(
        self, value=None, *, status=200, raw=None, retry="60", after_read=None
    ):
        self.status, self.retry, self.after_read = status, retry, after_read
        self.raw = json.dumps(value).encode() if raw is None else raw
        self.read_limits = []

    def getheader(self, name, default=None):
        assert name == "Retry-After"
        return self.retry if self.retry is not None else default

    def read(self, limit):
        self.read_limits.append(limit)
        if self.after_read:
            self.after_read()
        return self.raw[:limit]


class Harness:
    def __init__(self, *, overrides=None, spacing=0.6, seconds=300, wall=END):
        self.now, self.wall = 0, wall
        self.sleeps, self.calls, self.connections = [], [], []
        self.permits, self.penalties = [], []
        self.overrides = overrides or {}
        self.allow = True
        self.permit_error = self.penalty_error = self.factory_error = (
            self.close_error
        ) = False
        self.request_errors = set()
        self.market = FlowMarket(
            self,
            factory=self.factory,
            clock_ms=lambda: self.wall,
            monotonic=lambda: self.now,
            sleep=self.sleep,
            request_spacing=spacing,
            max_seconds=seconds,
        )

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def permit(self, weight):
        self.permits.append(weight)
        if self.permit_error:
            raise RuntimeError("secret quota detail")
        return self.allow

    def penalize(self, seconds):
        self.penalties.append(seconds)
        if self.penalty_error:
            raise RuntimeError("secret penalty detail")

    def factory(self, host, *, timeout):
        assert host in {"fapi.binance.com", "data-api.binance.vision"}
        assert timeout == 5
        if self.factory_error:
            raise RuntimeError("secret factory detail")
        connection = Connection(self, host)
        self.connections.append(connection)
        return connection

    def collect(self, symbol="BTCUSDT", **changes):
        return self.market.collect(
            symbol, **{"start_ms": START, "end_ms": END, **changes}
        )


class Connection:
    def __init__(self, harness, host):
        self.harness, self.host = harness, host
        self.closed = False

    def request(self, method, url):
        assert method == "GET"
        self.path = urlsplit(url).path
        self.query = parse_qs(urlsplit(url).query)
        self.harness.calls.append((self.host, method, self.path, self.query))
        if self.path in self.harness.request_errors:
            raise RuntimeError("secret request detail")

    def getresponse(self):
        if self.path in self.harness.overrides:
            return self.harness.overrides[self.path]
        symbol = self.query.get("symbol", ["BTCUSDT"])[0]
        if self.path in (FUTURES_CATALOG, SPOT_CATALOG):
            return Response({"symbols": [symbol_row(), symbol_row("ETHUSDT", "ETH")]})
        return Response(
            {SPOT: spot_rows(), OI: oi_rows(symbol), FUNDING: funding_rows(symbol)}[
                self.path
            ]
        )

    def close(self):
        self.closed = True
        if self.harness.close_error:
            raise RuntimeError("secret close detail")


def test_complete_bundle_preserves_original_rows_fixed_hosts_and_weights():
    harness = Harness()
    bundle = harness.collect()
    assert bundle["version"] == "mover-flow-public-v1"
    assert bundle["source"] == "BINANCE_LIVE_PUBLIC"
    assert bundle["symbol"] == "BTCUSDT"
    assert (bundle["start_ms"], bundle["end_ms"]) == (START, END)
    assert bundle["mapping"] == {
        "status": "MATCHED",
        "basis": "CURRENT_EXACT_SYMBOL_BASE_QUOTE",
        "spot_symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
    }
    for kind, rows, source in (
        ("spot", spot_rows(), "BINANCE_SPOT_LIVE"),
        ("open_interest", oi_rows(), "BINANCE_USDM_LIVE"),
        ("funding", funding_rows(), "BINANCE_USDM_LIVE"),
    ):
        assert bundle[kind] == {
            "status": "OBSERVED",
            "source": source,
            "fetched_at_ms": END,
            "rows": rows,
        }
    assert harness.permits == [1, 20, 2, 1, 1]
    assert harness.market.requests == 5 and harness.market.weight == 25
    assert harness.calls[0][3] == {}
    assert harness.calls[1][3] == {"showPermissionSets": ["false"]}
    assert harness.sleeps == pytest.approx([0.6, 12, 1.2, 0.6])
    assert all(connection.closed for connection in harness.connections)
    assert [host for host, _, _, _ in harness.calls] == [
        "fapi.binance.com",
        "data-api.binance.vision",
        "data-api.binance.vision",
        "fapi.binance.com",
        "fapi.binance.com",
    ]
    for _, _, path, query in harness.calls[2:]:
        assert query == {key: [str(value)] for key, value in params(path).items()}


def test_catalogs_are_fetched_lazily_and_once_for_multiple_symbols():
    harness = Harness()
    assert not harness.calls
    harness.collect()
    harness.collect("ETHUSDT")
    assert [path for _, _, path, _ in harness.calls].count(FUTURES_CATALOG) == 1
    assert [path for _, _, path, _ in harness.calls].count(SPOT_CATALOG) == 1
    assert harness.market.requests == 8


@pytest.mark.parametrize("path", [FUTURES_CATALOG, SPOT_CATALOG])
def test_failed_catalog_is_cached_and_does_not_block_futures_components(path):
    harness = Harness(overrides={path: Response(status=503)})
    for symbol in ("BTCUSDT", "ETHUSDT"):
        bundle = harness.collect(symbol)
        assert bundle["mapping"]["status"] == bundle["spot"]["status"] == "UNKNOWN"
        assert bundle["mapping"]["reason"] == "MOVER_PUBLIC_HTTP_ERROR"
        assert bundle["spot"]["rows"] == []
        assert (
            bundle["open_interest"]["status"]
            == bundle["funding"]["status"]
            == "OBSERVED"
        )
    assert [p for _, _, p, _ in harness.calls].count(path) == 1
    assert SPOT not in [p for _, _, p, _ in harness.calls]


@pytest.mark.parametrize(
    "futures,spot,symbol,reason",
    [
        (
            [symbol_row("1000SHIBUSDT", "1000SHIB")],
            [symbol_row("SHIBUSDT", "SHIB")],
            "1000SHIBUSDT",
            "NO_EXACT_SPOT_MAPPING",
        ),
        (
            [symbol_row()],
            [symbol_row(base="OTHER")],
            "BTCUSDT",
            "EXACT_SPOT_ASSET_MISMATCH",
        ),
        (
            [symbol_row()],
            [symbol_row(quoteAsset="USDC")],
            "BTCUSDT",
            "NO_EXACT_SPOT_MAPPING",
        ),
        (
            [symbol_row(contractType="CURRENT_QUARTER")],
            [symbol_row()],
            "BTCUSDT",
            "NO_EXACT_SPOT_MAPPING",
        ),
    ],
)
def test_mapping_requires_exact_symbol_and_assets_without_prefix_heuristics(
    futures, spot, symbol, reason
):
    harness = Harness(
        overrides={
            FUTURES_CATALOG: Response({"symbols": futures}),
            SPOT_CATALOG: Response({"symbols": spot}),
        }
    )
    bundle = harness.collect(symbol)
    assert bundle["mapping"]["reason"] == reason
    assert bundle["spot"]["status"] == "UNKNOWN"
    assert (
        bundle["open_interest"]["status"] == bundle["funding"]["status"] == "OBSERVED"
    )


@pytest.mark.parametrize(
    "path,kind", [(SPOT, "spot"), (OI, "open_interest"), (FUNDING, "funding")]
)
@pytest.mark.parametrize(
    "response,reason",
    [
        (Response(status=503), "MOVER_PUBLIC_HTTP_ERROR"),
        (Response(status=302), "MOVER_PUBLIC_HTTP_ERROR"),
        (Response({"code": -1, "msg": "secret payload"}), "MOVER_PUBLIC_API_ERROR"),
        (Response(raw=b"secret malformed body"), "MOVER_PUBLIC_TRANSPORT_FAILURE"),
        (Response([]), "MOVER_FLOW_ROWS_EMPTY"),
        (Response({"unexpected": "secret"}), "MOVER_FLOW_ROWS_INVALID"),
    ],
)
def test_component_errors_are_sanitized_and_isolated(path, kind, response, reason):
    harness = Harness(overrides={path: response})
    bundle = harness.collect()
    assert bundle[kind]["status"] == "UNKNOWN"
    assert bundle[kind]["reason"] == reason and bundle[kind]["rows"] == []
    assert "secret" not in json.dumps(bundle)
    assert all(
        bundle[other]["status"] == "OBSERVED"
        for other in {"spot", "open_interest", "funding"} - {kind}
    )


@pytest.mark.parametrize(
    "path,query",
    [
        ("/fapi/v1/order", {}),
        ("/fapi/v2/positionRisk", {}),
        ("/api/v3/account", {}),
        ("/fapi/v1/time", {}),
        ("https://evil.example/api/v3/klines", {}),
        (SPOT_CATALOG, {"apiKey": "secret"}),
        (SPOT_CATALOG, {}),
        (SPOT_CATALOG, {"showPermissionSets": "true"}),
        (SPOT_CATALOG, {"showPermissionSets": False}),
        (SPOT_CATALOG, {"showPermissionSets": "false", "symbol": "BTCUSDT"}),
        (SPOT_CATALOG, {"showPermissionSets": "false", "apiKey": "secret"}),
        (FUTURES_CATALOG, {"showPermissionSets": "false"}),
        (FUTURES_CATALOG, {"signature": "secret"}),
        (SPOT, params(signature="secret")),
        (FUNDING, params(FUNDING, recvWindow=1)),
    ],
)
def test_only_fixed_unsigned_public_gets_can_spend_quota(path, query):
    harness = Harness()
    with pytest.raises(ValueError, match="MOVER_PUBLIC_ENDPOINT_DISABLED"):
        harness.market.get(path, query)
    assert not harness.calls and not harness.permits


@pytest.mark.parametrize(
    "path,changes",
    [
        (SPOT, {"limit": 25}),
        (SPOT, {"interval": "1m"}),
        (SPOT, {"startTime": True}),
        (SPOT, {"symbol": "BTCUSDT&signature=secret"}),
        (SPOT, {"endTime": END}),
        (OI, {"limit": 31}),
        (OI, {"period": "5m"}),
        (OI, {"endTime": END - 1}),
        (FUNDING, {"limit": 201}),
        (FUNDING, {"startTime": START - 2 * DAY}),
    ],
)
def test_endpoint_bounds_are_checked_before_quota(path, changes):
    harness = Harness()
    with pytest.raises(ValueError):
        harness.market.get(path, params(path, **changes))
    assert not harness.calls and not harness.permits


@pytest.mark.parametrize(
    "start,end,wall,reason",
    [
        (START, END, END - 1, "MOVER_DAY_NOT_CLOSED"),
        (START + 1, END + 1, END + DAY, "MOVER_CLOSED_DAY_REQUIRED"),
        (START, END + DAY, END + DAY, "MOVER_CLOSED_DAY_REQUIRED"),
        (START, END, END + 3 * DAY, "MOVER_FLOW_RECENT_DAY_REQUIRED"),
        (True, END, END, "MOVER_CLOSED_DAY_REQUIRED"),
    ],
)
def test_only_last_three_completed_utc8_days_are_accepted(start, end, wall, reason):
    harness = Harness(wall=wall)
    with pytest.raises(ValueError, match=reason):
        harness.collect(start_ms=start, end_ms=end)
    assert not harness.calls and not harness.permits


def test_third_completed_day_remains_eligible_until_next_utc8_midnight():
    harness = Harness(wall=END + 3 * DAY - 1)
    assert harness.collect()["spot"]["status"] == "OBSERVED"


@pytest.mark.parametrize(
    "status,retry,penalty",
    [
        (429, "1", 60),
        (418, "300", 300),
        (429, "99999999", 259200),
        (429, "bad", 60),
        (418, None, 60),
    ],
)
def test_rate_limit_penalizes_once_and_latches_whole_instance_halt(
    status, retry, penalty
):
    harness = Harness(overrides={OI: Response(status=status, retry=retry)})
    harness.penalty_error = harness.close_error = True
    for _ in range(2):
        with pytest.raises(MoverMarketError, match="^MOVER_RATE_LIMITED$"):
            harness.collect()
    assert harness.penalties == [penalty]
    assert len(harness.calls) == 4
    assert FUNDING not in [path for _, _, path, _ in harness.calls]
    assert all(connection.closed for connection in harness.connections)


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("allow", "MOVER_SHARED_QUOTA_WAIT"),
        ("permit_error", "MOVER_SHARED_QUOTA_FAILURE"),
    ],
)
def test_quota_denial_and_failure_halt_without_transport(failure, reason):
    harness = Harness()
    setattr(harness, failure, failure != "allow")
    for _ in range(2):
        with pytest.raises(MoverMarketError, match=f"^{reason}$"):
            harness.collect()
    assert harness.permits == [1] and not harness.connections


def test_deadline_accounts_for_weight_spacing_before_permit():
    harness = Harness(seconds=30, spacing=5)
    with pytest.raises(MoverMarketError, match="MOVER_JOB_DEADLINE"):
        harness.collect()
    assert harness.permits == [1, 20]
    assert harness.sleeps == [5]
    assert len(harness.calls) == 2


def test_expired_deadline_after_body_read_halts_instead_of_returning_observation():
    harness = Harness(seconds=30)
    harness.overrides[SPOT] = Response(
        spot_rows(), after_read=lambda: setattr(harness, "now", 31)
    )
    with pytest.raises(MoverMarketError, match="MOVER_JOB_DEADLINE"):
        harness.collect()
    assert len(harness.calls) == 3


def test_lifetime_request_limit_is_180_even_with_time_remaining():
    harness = Harness(spacing=0.5)
    for _ in range(180):
        harness.market.get(FUTURES_CATALOG, {})
    with pytest.raises(MoverMarketError, match="MOVER_FLOW_REQUEST_LIMIT"):
        harness.market.get(FUTURES_CATALOG, {})
    assert len(harness.calls) == len(harness.permits) == harness.market.requests == 180


@pytest.mark.parametrize(
    "path,cap",
    [
        (SPOT_CATALOG, 8_000_000),
        (FUTURES_CATALOG, 4_000_000),
        (SPOT, 128_000),
        (OI, 128_000),
        (FUNDING, 128_000),
    ],
)
def test_response_body_has_endpoint_specific_hard_cap(path, cap):
    response = Response(raw=b"x" * (cap + 1))
    harness = Harness(overrides={path: response})
    with pytest.raises(MoverMarketError, match="MOVER_RESPONSE_TOO_LARGE"):
        harness.market.get(
            path,
            {"showPermissionSets": "false"}
            if path == SPOT_CATALOG
            else {}
            if path == FUTURES_CATALOG
            else params(path),
        )
    assert response.read_limits == [cap + 1]
    assert harness.connections[0].closed


@pytest.mark.parametrize(
    "path,value",
    [
        (SPOT_CATALOG, {"symbols": [symbol_row()] * 10_001}),
        (FUTURES_CATALOG, {"symbols": [symbol_row()] * 5_001}),
        (SPOT_CATALOG, {"symbols": [symbol_row(), symbol_row()]}),
        (SPOT_CATALOG, {"symbols": [symbol_row(base="bad\nasset")]}),
        (SPOT_CATALOG, {"symbols": [{}]}),
        (FUTURES_CATALOG, {"symbols": [symbol_row(contractType=None)]}),
        (SPOT_CATALOG, {"symbols": []}),
    ],
)
def test_catalog_row_bounds_and_identity_fail_closed(path, value):
    harness = Harness(overrides={path: Response(value)})
    assert harness.collect()["mapping"]["reason"] == "MOVER_FLOW_CATALOG_INVALID"


@pytest.mark.parametrize(
    "path,kind,value",
    [
        (SPOT, "spot", spot_rows() + spot_rows()[:1]),
        (SPOT, "spot", [spot_rows()[0][:-1]]),
        (SPOT, "spot", [[True, *spot_rows()[0][1:]]]),
        (OI, "open_interest", oi_rows() * 2),
        (OI, "open_interest", [{**oi_rows()[0], "timestamp": "123"}]),
        (OI, "open_interest", [{**oi_rows()[0], "unexpected": {}}]),
        (FUNDING, "funding", funding_rows() * 30),
        (FUNDING, "funding", [{**funding_rows()[0], "fundingRate": "1" * 129}]),
        (FUNDING, "funding", [{"symbol": "BTCUSDT", "fundingTime": START}]),
    ],
)
def test_data_shape_and_row_limits_are_strict(path, kind, value):
    harness = Harness(overrides={path: Response(value)})
    assert harness.collect()[kind]["reason"] == "MOVER_FLOW_ROWS_INVALID"


def test_failed_transport_consumes_attempt_weight_without_exposing_exception_text():
    harness = Harness()
    harness.request_errors.add(OI)
    bundle = harness.collect()
    assert bundle["open_interest"]["reason"] == "MOVER_PUBLIC_TRANSPORT_FAILURE"
    assert "secret" not in json.dumps(bundle)
    assert harness.market.requests == len(harness.permits) == 5
    assert bundle["funding"]["status"] == "OBSERVED"


def test_failed_factory_is_counted_and_sanitized():
    harness = Harness()
    harness.factory_error = True
    with pytest.raises(MoverMarketError, match="^MOVER_PUBLIC_TRANSPORT_FAILURE$"):
        harness.market.get(FUTURES_CATALOG, {})
    assert harness.market.requests == 1 and harness.permits == [1]


@pytest.mark.parametrize(
    "changes",
    [
        {"spacing": 0.49},
        {"spacing": True},
        {"seconds": 301},
        {"seconds": 29},
        {"seconds": float("nan")},
    ],
)
def test_constructor_limits_cannot_disable_pacing_or_deadline(changes):
    with pytest.raises(ValueError, match="MOVER_FLOW_LIMIT_INVALID"):
        Harness(**changes)
