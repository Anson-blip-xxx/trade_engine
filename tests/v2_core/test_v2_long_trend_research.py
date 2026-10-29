"""Public adapter and full research path; all HTTP is fake, no venue access."""

import json
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from services import v2_long_trend_research as research
from v2_core.long_trend import LongTrendPolicy

HOUR, DAY = research.HOUR, research.DAY


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("real network prohibited in research QA")

    monkeypatch.setattr(research, "HTTPSConnection", denied)


def candle(at, price="100"):
    p = Decimal(price)
    return [at, str(p), str(p + 1), str(p - 1), str(p), "1000", at + HOUR - 1]


def funding(at, *, rate="0.001", symbol="BTCUSDT"):
    return {
        "symbol": symbol,
        "fundingTime": at,
        "fundingRate": rate,
        "markPrice": "100",
    }


def contract(symbol="BTCUSDT", onboard=0):
    return {
        "symbol": symbol,
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "quoteAsset": "USDT",
        "onboardDate": onboard,
        "filters": [
            {
                "filterType": "LOT_SIZE",
                "stepSize": "0.001",
                "minQty": "0.001",
                "maxQty": "1000",
            },
            {
                "filterType": "MARKET_LOT_SIZE",
                "stepSize": "0.01",
                "minQty": "0.01",
                "maxQty": "100",
            },
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ],
    }


class History:
    def __init__(self, events=(), *, rising=False):
        self.events, self.calls, self.rising = list(events), [], rising

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        start, end = params["startTime"], params["endTime"]
        if path.endswith("klines"):
            return [
                candle(at, str(100 + at // DAY) if self.rising else "100")
                for at in range(start, min(end + 1, start + 1000 * HOUR), HOUR)
            ]
        assert path.endswith("fundingRate")
        return [e for e in self.events if start <= e["fundingTime"] <= end][:1000]


def history(public, *, end=2 * HOUR, now=None):
    return research.read_history(
        public, "BTCUSDT", start_ms=0, end_ms=end, now_ms=end if now is None else now
    )


def test_history_pages_contiguous_bars_and_exact_funding_cursors():
    # Funding need not land on the hourly bar boundary: preserve millisecond time.
    public = History([funding(i * 1000 + 17) for i in range(1001)])
    bars, events, coverage = history(public, end=1001 * HOUR)
    assert len(bars) == 1001 and len(events) == 1001
    assert [b.t for b in bars] == list(range(0, 1001 * HOUR, HOUR))
    klines = [p for path, p in public.calls if path.endswith("klines")]
    funds = [p for path, p in public.calls if path.endswith("fundingRate")]
    assert [p["startTime"] for p in klines] == [0, 1000 * HOUR]
    assert [p["startTime"] for p in funds] == [0, 999018]
    assert all(
        p["endTime"] == 1001 * HOUR - 1 and p["limit"] == 1000 for _, p in public.calls
    )
    assert events[0]["at_ms"] == 17
    assert coverage["funding_history_fetch_complete"] is True
    assert coverage["historical_funding_schedule_verified"] is False
    assert coverage["funding_pages"] == 2
    assert (
        coverage["digest"]
        == history(History(public.events), end=1001 * HOUR)[2]["digest"]
    )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        {},
        [candle(HOUR)],
        [candle(0), candle(0)],
        [candle(0), candle(2 * HOUR)],
        [candle(0)[:-1]],
        [candle(0)[:6] + [HOUR]],
        [candle(0), candle(HOUR), candle(2 * HOUR)],
        [[True, "100", "101", "99", "100", "1", HOUR - 1]],
        [candle(0)] * 1001,
    ],
)
def test_history_rejects_gaps_duplicates_future_and_bad_shape(rows):
    with pytest.raises(ValueError):
        history(lambda path, params: rows)


@pytest.mark.parametrize("field,value", [(1, "NaN"), (2, "1"), (5, "-1"), (4, 100.1)])
def test_history_rejects_invalid_price_data(field, value):
    row = candle(0)
    row[field] = value
    with pytest.raises(ValueError):
        history(lambda path, params: [row], end=HOUR)


@pytest.mark.parametrize(
    "changes",
    [
        {"start_ms": 1},
        {"end_ms": HOUR - 1},
        {"now_ms": HOUR - 1},
        {"start_ms": True},
        {"end_ms": 181 * DAY, "now_ms": 181 * DAY},
    ],
)
def test_history_bad_bounds_do_not_request(changes):
    kwargs = {"start_ms": 0, "end_ms": HOUR, "now_ms": HOUR}
    kwargs.update(changes)
    public = History()
    with pytest.raises(ValueError, match="RESEARCH_HISTORY_BOUND"):
        research.read_history(public, "BTCUSDT", **kwargs)
    assert public.calls == []


@pytest.mark.parametrize(
    "rows",
    [
        {},
        [funding(0), funding(0)],
        [funding(10), funding(9)],
        [funding(2 * HOUR)],
        [funding(-1)],
        [funding(0, symbol="ETHUSDT")],
        [funding(0, rate="NaN")],
        [funding(0, rate="1.01")],
        [{**funding(0), "markPrice": "0"}],
        [{**funding(0), "fundingTime": True}],
        [{"fundingTime": 0}],
        [None],
        [funding(0)] * 1001,
    ],
)
def test_funding_rejects_malformed_duplicate_outoforder_and_boundary(rows):
    real = History()

    def public(path, params):
        return real(path, params) if path.endswith("klines") else rows

    with pytest.raises(ValueError):
        history(public)


def test_funding_nonadvancing_page_cannot_loop():
    real = History()
    pages = 0

    def public(path, params):
        nonlocal pages
        if path.endswith("klines"):
            return real(path, params)
        pages += 1
        return [funding(i) for i in range(1000)]

    with pytest.raises(ValueError, match="RESEARCH_FUNDING_INVALID"):
        history(public)
    assert pages == 2


def test_funding_page_limit_is_bounded():
    public = History([funding(i) for i in range(11_000)])
    with pytest.raises(ValueError, match="RESEARCH_FUNDING_PAGE_BOUND"):
        history(public)
    assert len([p for path, p in public.calls if path.endswith("fundingRate")]) == 10


def test_catalog_cohort_is_order_independent_not_performance_ranked():
    rows = [contract(f"COIN{i}USDT", onboard=0) for i in range(25)]
    rows += [contract("NEWUSDT", onboard=2 * DAY), contract("BTCUSDT")]
    rows[2]["status"] = "SETTLING"
    rows[3]["quoteAsset"] = "USDC"
    rows[4]["contractType"] = "CURRENT_QUARTER"
    first = research.select_cohort(
        {"symbols": rows}, required=("BTCUSDT",), count=12, listed_before_ms=DAY
    )
    second = research.select_cohort(
        {"symbols": list(reversed(rows))},
        required=("BTCUSDT",),
        count=12,
        listed_before_ms=DAY,
    )
    assert first == second and len(first) == len(set(first)) == 12
    assert first[0] == "BTCUSDT"
    assert not set(first) & {"NEWUSDT", "COIN2USDT", "COIN3USDT", "COIN4USDT"}


def test_cjk_mainnet_asset_is_retained_in_public_catalog_only():
    target = "币安人生USDT"
    catalog = {"symbols": [contract(target), contract()]}
    assert set(research.catalog_symbols(catalog)) == {target, "BTCUSDT"}
    assert research.select_cohort(
        catalog, required=(target,), count=1, listed_before_ms=DAY
    ) == (target,)


@pytest.mark.parametrize(
    "target",
    [
        "币安人生USDT&signature=x",
        "币/安USDT",
        "币安\nUSDT",
        "币安%26USDT",
        "币安人生USDC",
    ],
)
def test_unicode_symbol_does_not_allow_url_injection(target):
    with pytest.raises(ValueError, match="RESEARCH_SYMBOL_INVALID"):
        research.validate_symbol(target)


@pytest.mark.parametrize(
    "catalog",
    [
        None,
        {},
        {"symbols": {}},
        {"symbols": [None]},
        {"symbols": ["BAD"]},
        {"symbols": [contract(), contract()]},
    ],
)
def test_catalog_bad_rows_fail_closed(catalog):
    with pytest.raises(ValueError):
        research.catalog_symbols(catalog)


def test_unavailable_required_symbol_never_silently_substituted():
    with pytest.raises(ValueError, match="RESEARCH_REQUIRED_SYMBOL_UNAVAILABLE"):
        research.select_cohort(
            {"symbols": [contract()]},
            required=("BTWUSDT",),
            count=1,
            listed_before_ms=DAY,
        )


def test_current_venue_filters_conservatively_intersect_market_and_lot_rules():
    result = research.venue_policy(contract(), LongTrendPolicy())
    assert result.quantity_step == "0.01"
    assert result.minimum_quantity == "0.01" and result.maximum_quantity == "100"
    assert result.price_tick == "0.01" and result.minimum_notional == "5"


@pytest.mark.parametrize(
    "case", ["missing", "non_nested", "zero", "duplicate", "malformed"]
)
def test_unsafe_venue_filters_rejected(case):
    row = contract()
    if case == "missing":
        row["filters"] = []
    elif case == "non_nested":
        row["filters"][0]["stepSize"] = "0.003"
    elif case == "zero":
        row["filters"][2]["tickSize"] = "0"
    elif case == "duplicate":
        row["filters"].append({"filterType": "PRICE_FILTER", "tickSize": "1"})
    else:
        row["filters"] = [None]
    with pytest.raises(ValueError, match="RESEARCH_VENUE_FILTERS"):
        research.venue_policy(row, LongTrendPolicy())


def test_fake_public_to_real_replay_complete_flow_labels_nonacceptance():
    public = History(rising=True)
    report = research.study(
        public,
        catalog={"symbols": [contract()]},
        symbols=("BTCUSDT", "DEADUSDT"),
        start_ms=30 * DAY,
        end_ms=60 * DAY,
        now_ms=60 * DAY + 17,
    )
    assert (
        report["execution_authorized"] is False
        and report["profitability_valid"] is False
    )
    assert report["execution_environment"] == "NONE"
    assert report["market_environment"] == "LIVE_PUBLIC_ONLY"
    assert report["coverage_complete"] is False
    assert report["unprocessed_symbols"] == []
    assert "CURRENT_CATALOG_SURVIVORSHIP_BIAS" in report["assumptions"]
    assert "LATER_PERIOD_NOT_TRUE_UNSEEN_HOLDOUT" in report["assumptions"]
    assert "PER_SYMBOL_CAPITAL_IS_NOT_AN_AGGREGATE_PORTFOLIO" in report["assumptions"]
    assert report["asof_utc8"].endswith("+08:00")
    assert report["reports"][1] == {
        "symbol": "DEADUSDT",
        "status": "NOT_IN_CURRENT_MAINNET_CATALOG",
    }
    first = report["reports"][0]
    assert first["status"] == "SIMULATED"
    assert first["coverage"]["bars"] == 51 * 24
    assert first["coverage"]["historical_funding_schedule_verified"] is False
    assert set(first["folds"]) == {"initial", "later_reset", "full"}
    for fold in first["folds"].values():
        assert "curve" not in fold and "decisions" not in fold
        assert fold["execution_authorized"] is False
    assert all(p["symbol"] == "BTCUSDT" for _, p in public.calls)
    assert json.loads(json.dumps(report)) == report


def test_study_missing_history_is_not_counted_as_a_success():
    report = research.study(
        lambda path, params: [],
        catalog={"symbols": [contract()]},
        symbols=("BTCUSDT",),
        start_ms=30 * DAY,
        end_ms=60 * DAY,
        now_ms=60 * DAY,
    )
    assert report["reports"] == [
        {
            "symbol": "BTCUSDT",
            "status": "UNCOVERED",
            "reason": "RESEARCH_HISTORY_INCOMPLETE",
        }
    ]
    assert report["coverage_complete"] is False
    assert report["unprocessed_symbols"] == []


def test_complete_bar_coverage_still_does_not_validate_profitability():
    report = research.study(
        History(),
        catalog={"symbols": [contract()]},
        symbols=("BTCUSDT",),
        start_ms=30 * DAY,
        end_ms=60 * DAY,
        now_ms=60 * DAY,
    )
    assert report["coverage_complete"] is True
    assert report["unprocessed_symbols"] == []
    assert report["profitability_valid"] is False
    assert report["reports"][0]["folds"]["full"]["funding_complete"] is False


def test_study_stops_entire_job_after_rate_budget_guard():
    class Stopped:
        blocked = False

        def __call__(self, path, params):
            self.blocked = True
            raise research.ResearchDataError("RESEARCH_RATE_LIMITED")

    report = research.study(
        Stopped(),
        catalog={"symbols": [contract(), contract("ETHUSDT")]},
        symbols=("BTCUSDT", "ETHUSDT"),
        start_ms=30 * DAY,
        end_ms=60 * DAY,
        now_ms=60 * DAY,
    )
    assert len(report["reports"]) == 1
    assert report["reports"][0]["reason"] == "RESEARCH_RATE_LIMITED"
    assert report["requested_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert report["coverage_complete"] is False
    assert report["unprocessed_symbols"] == ["ETHUSDT"]


class Response:
    def __init__(self, body=b'{"serverTime":123}', status=200, weight="0"):
        self.body, self.status, self.weight = body, status, weight

    def getheader(self, name, default=None):
        assert name == "X-MBX-USED-WEIGHT-1M"
        return self.weight

    def read(self, maximum):
        return self.body[:maximum]


def install_http(monkeypatch, response):
    calls, closed = [], []

    class Connection:
        def __init__(self, host, timeout):
            assert host == "fapi.binance.com" and timeout == 5

        def request(self, method, path, headers):
            calls.append((method, path, headers))

        def getresponse(self):
            return response

        def close(self):
            closed.append(True)

    monkeypatch.setattr(research, "HTTPSConnection", Connection)
    return calls, closed


def params():
    return {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "startTime": 0,
        "endTime": HOUR - 1,
        "limit": 1000,
    }


@pytest.mark.parametrize(
    "path,query",
    [
        ("/fapi/v1/order", {}),
        ("https://evil.example/", {}),
        ("/fapi/v2/account", {}),
        ("/fapi/v1/time", {"signature": "secret"}),
        ("/fapi/v1/klines", {**params(), "limit": 1500}),
        ("/fapi/v1/klines", {**params(), "interval": "1m"}),
        ("/fapi/v1/klines", {**params(), "recvWindow": 1000}),
        ("/fapi/v1/klines", {**params(), "startTime": True}),
        ("/fapi/v1/klines", {**params(), "symbol": "BTCUSDT&signature=secret"}),
    ],
)
def test_transport_denies_private_or_unbounded_endpoints_before_http(path, query):
    client = research.PublicResearchTransport()
    with pytest.raises(ValueError):
        client(path, query)
    assert client.requests == client.weight == 0


def test_transport_only_public_get_headers_and_always_closes(monkeypatch):
    calls, closed = install_http(monkeypatch, Response())
    client = research.PublicResearchTransport()
    assert client("/fapi/v1/time", {}) == {"serverTime": 123}
    assert calls == [("GET", "/fapi/v1/time", {"Accept": "application/json"})]
    assert closed == [True] and client.requests == client.weight == 1


def test_public_unicode_symbol_is_percent_encoded_without_changing_endpoint(
    monkeypatch,
):
    calls, closed = install_http(monkeypatch, Response(body=b"[]"))
    client = research.PublicResearchTransport()
    query = {**params(), "symbol": "币安人生USDT"}
    assert client("/fapi/v1/klines", query) == []
    parsed = urlsplit(calls[0][1])
    assert parsed.path == "/fapi/v1/klines"
    assert parse_qs(parsed.query)["symbol"] == ["币安人生USDT"]
    assert "%" in parsed.query and "币" not in parsed.query
    assert closed == [True]


@pytest.mark.parametrize(
    "response,code,blocks",
    [
        (Response(status=418), "RESEARCH_RATE_LIMITED", True),
        (Response(status=429), "RESEARCH_RATE_LIMITED", True),
        (Response(weight="1000"), "RESEARCH_SHARED_IP_BUSY", True),
        (Response(status=302), "RESEARCH_HTTP_ERROR", False),
        (Response(status=500), "RESEARCH_HTTP_ERROR", False),
        (Response(body=b'{"code":-1000}'), "RESEARCH_API_ERROR", False),
        (Response(body=b"123"), "RESEARCH_API_ERROR", False),
        (Response(body=b'{"a":1,"a":2}'), "RESEARCH_TRANSPORT_FAILURE", False),
        (Response(body=b'{"a":NaN}'), "RESEARCH_TRANSPORT_FAILURE", False),
        (Response(body=b"x" * 2_000_001), "RESEARCH_RESPONSE_TOO_LARGE", False),
    ],
)
def test_transport_errors_are_fixed_codes_and_stop_when_required(
    monkeypatch, response, code, blocks
):
    calls, closed = install_http(monkeypatch, response)
    client = research.PublicResearchTransport()
    with pytest.raises(ValueError, match=code):
        client("/fapi/v1/time", {})
    assert closed == [True] and client.blocked is blocks
    if blocks:
        with pytest.raises(ValueError, match="RESEARCH_JOB_STOPPED"):
            client("/fapi/v1/time", {})
        assert len(calls) == 1


@pytest.mark.parametrize("bound", ["requests", "weight", "deadline"])
def test_transport_budget_exhaustion_is_permanent(monkeypatch, bound):
    calls, _ = install_http(monkeypatch, Response())
    client = research.PublicResearchTransport(max_requests=1, max_weight=5)
    if bound == "requests":
        client("/fapi/v1/time", {})
    elif bound == "weight":
        client.weight = 5
    else:
        client.deadline = -1
    with pytest.raises(ValueError, match="RESEARCH_REQUEST_BUDGET"):
        client("/fapi/v1/klines", params())
    before = len(calls)
    with pytest.raises(ValueError, match="RESEARCH_JOB_STOPPED"):
        client("/fapi/v1/time", {})
    assert len(calls) == before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_requests": True},
        {"max_requests": 101},
        {"max_weight": 251},
        {"deadline_seconds": 301},
        {"deadline_seconds": 0},
    ],
)
def test_transport_constructor_bounds(kwargs):
    with pytest.raises(ValueError, match="RESEARCH_TRANSPORT_BOUNDS"):
        research.PublicResearchTransport(**kwargs)
