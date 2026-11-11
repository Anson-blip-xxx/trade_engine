import copy
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from services import v2_onchain_market as module
from services.v2_onchain_market import CAP, HOST, SOURCE, VERSION, OnchainMarket
from v2_core.evidence import canonical, digest

NOW = 1_793_438_790_000
ADDRESS = "0xc07e1300dc138601fa6b0b59f8d0fa477e690589"
OTHER = "0x55d398326f99059ff775485246999027b3197955"
SOL = "So11111111111111111111111111111111111111112"
SOL_OTHER = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def token(network="bsc", address=ADDRESS):
    return {
        "id": address,
        "chain": network,
        "symbol": "Q",
        "name": "Quack AI",
        "decimals": 18,
        "no_index": False,
        "description": "provider prose must not enter evidence",
        "website": "https://untrusted.invalid",
        "summary": {
            "chain": network,
            "id": address,
            "price_usd": 0.0317,
            "liquidity_usd": 421984.2,
            "pools": 1522,
            "5m": {
                "volume": 0,
                "volume_usd": 0,
                "buy_usd": 0,
                "sell_usd": 0,
                "buys": 0,
                "sells": 0,
                "txns": 0,
                "last_price_usd_change": -2.1,
            },
            "1h": {"buys": 0, "sells": 0, "txns": 0},
        },
        "last_updated": "2026-10-31T09:46:30Z",
    }


def pools(network="bsc", address=ADDRESS):
    other = SOL_OTHER if network == "solana" else OTHER
    return {
        "results": [
            {
                "id": "0x" + "e" * 40,
                "chain": network,
                "dex_id": "pancakeswap_v3",
                "dex_name": "PancakeSwap V3",
                "volume_usd_24h": 100.125,
                "liquidity_usd": 0,
                "price_usd": 2.5,
                "transactions_24h": 7,
                "created_at": "2026-10-01T00:00:00Z",
                "tokens": [
                    {"id": address, "chain": network, "has_image": True},
                    {"id": other, "chain": network},
                ],
            }
        ],
        "has_next_page": True,
        "next_cursor": "must not be followed automatically",
        "query": {
            "network": network,
            "token_address": address,
            "limit": 3,
            "order_by": "volume_usd_24h",
            "sort": "desc",
        },
    }


class Response:
    def __init__(self, value=None, *, status=200, raw=None, headers=None):
        self.status = status
        self.raw = json.dumps(value).encode() if raw is None else raw
        self.headers = headers or {}
        self.read_sizes = []

    def read(self, limit):
        self.read_sizes.append(limit)
        return self.raw[:limit]

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class Harness:
    def __init__(self, overrides=None, **limits):
        self.now = 0
        self.overrides = overrides or {}
        self.calls, self.closed, self.sleeps = [], [], []
        self.network, self.address = "bsc", ADDRESS
        self.market = OnchainMarket(
            factory=self.factory,
            clock_ms=lambda: NOW + int(self.now * 1000),
            monotonic=lambda: self.now,
            sleep=self.sleep,
            **limits,
        )

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def factory(self, host, *, timeout):
        assert host == HOST and timeout <= 3
        harness = self

        class Connection:
            sock = None

            def request(self, method, path, *, headers):
                assert method == "GET"
                assert headers == {"Accept": "application/json"}
                self.kind = "pools" if "/pools/search" in path else "token"
                harness.calls.append((harness.now, host, method, path, headers))

            def getresponse(self):
                result = harness.overrides.get(self.kind)
                if isinstance(result, Exception):
                    raise result
                if callable(result):
                    return result()
                if result is not None:
                    return result
                return Response(
                    (token if self.kind == "token" else pools)(
                        harness.network, harness.address
                    )
                )

            def close(self):
                harness.closed.append(getattr(self, "kind", "unrequested"))

        return Connection()

    def collect(self):
        return self.market.collect(self.network, self.address, now_ms=NOW)


def reason(bundle, component="token"):
    return bundle["components"][component]["reason"]


def test_current_paths_identity_hash_and_decimal_contract():
    h = Harness()
    result = h.collect()
    assert result["version"] == VERSION
    assert result["source"] == SOURCE
    assert result["provider"] == "DEXPAPRIKA"
    assert result["network_id"] == "bsc"
    assert result["token_address"] == ADDRESS
    assert result["finished_at_ms"] - result["started_at_ms"] == 6000
    assert not result["execution_authorized"]
    assert not result["automatic_parameter_updates"]
    assert not result["promotion_authorized"]
    assert h.calls[0][3] == f"/networks/bsc/tokens/{ADDRESS}"
    path = urlsplit(h.calls[1][3])
    assert path.path == "/networks/bsc/pools/search"
    assert parse_qs(path.query) == {
        "token_address": [ADDRESS],
        "order_by": ["volume_usd_24h"],
        "sort": ["desc"],
        "limit": ["3"],
    }
    for component in result["components"].values():
        assert component["status"] == "OBSERVED"
        assert "?" not in component["endpoint"]
        assert component["sha256"] == digest(
            canonical({"payload": component["payload"]})
        )
    summary = result["components"]["token"]["payload"]["summary"]
    assert summary["id"] == ADDRESS
    assert summary["chain"] == "bsc"
    assert summary["price_usd"] == "0.0317"
    assert summary["5m"]["buys"] == 0
    assert summary["5m"]["buy_usd"] == "0"
    assert summary["5m"]["last_price_usd_change"] == "-2.1"
    # Missing amount is not reconstructed from the count, even if count is zero.
    assert "buy_usd" not in summary["1h"]
    assert "next_cursor" not in result["components"]["pools"]["payload"]
    assert "description" not in result["components"]["token"]["payload"]
    assert "website" not in result["components"]["token"]["payload"]
    canonical(result)


def test_environment_secrets_are_never_read_or_sent(monkeypatch):
    for key in ("API_KEY", "DEXPAPRIKA_API_KEY", "BINANCE_API_KEY", "HTTPS_PROXY"):
        monkeypatch.setenv(key, "do-not-read-or-send-this-secret")
    h = Harness()
    result = h.collect()
    assert "do-not-read" not in canonical(result)
    assert all(
        call[1] == HOST and call[4] == {"Accept": "application/json"}
        for call in h.calls
    )


def test_evm_is_normalized_and_solana_keeps_case():
    h = Harness()
    result = h.market.collect("bsc", "0x" + ADDRESS[2:].upper(), now_ms=NOW)
    assert result["token_address"] == ADDRESS
    h = Harness()
    h.network, h.address = "solana", SOL
    assert h.collect()["token_address"] == SOL
    assert SOL in h.calls[0][3]


@pytest.mark.parametrize(
    "network,address",
    [
        ("http://evil.invalid", ADDRESS),
        ("bsc/../../orders", ADDRESS),
        ("unsupported", ADDRESS),
        ("bsc", ADDRESS + "?apikey=bad"),
        ("bsc", "0x" + "1" * 39),
        ("bsc", "0x" + "1" * 41),
        ("solana", "0" * 40),
        ("bsc", None),
        (None, ADDRESS),
    ],
)
def test_invalid_path_input_is_rejected_before_network(network, address):
    h = Harness()
    with pytest.raises((ValueError, TypeError), match="ONCHAIN_"):
        h.market.collect(network, address, now_ms=NOW)
    assert not h.calls


@pytest.mark.parametrize(
    "limits",
    [
        {"max_requests": 7},
        {"max_requests": 0},
        {"max_requests": True},
        {"max_seconds": 51},
        {"max_seconds": 2},
        {"max_seconds": True},
        {"request_spacing": 5.99},
        {"request_spacing": float("nan")},
        {"request_spacing": float("inf")},
        {"request_spacing": True},
    ],
)
def test_safety_limits_cannot_be_expanded(limits):
    with pytest.raises(ValueError, match="ONCHAIN_LIMIT_INVALID"):
        Harness(**limits)


@pytest.mark.parametrize(
    "status,code",
    [
        (414, "ONCHAIN_RATE_LIMITED"),
        (418, "ONCHAIN_RATE_LIMITED"),
        (429, "ONCHAIN_RATE_LIMITED"),
        (401, "ONCHAIN_ACCESS_DENIED"),
        (403, "ONCHAIN_ACCESS_DENIED"),
        (402, "ONCHAIN_CREDIT_EXHAUSTED"),
        (301, "ONCHAIN_REDIRECT_BLOCKED"),
        (302, "ONCHAIN_REDIRECT_BLOCKED"),
        (307, "ONCHAIN_REDIRECT_BLOCKED"),
        (500, "ONCHAIN_PROVIDER_UNAVAILABLE"),
        (502, "ONCHAIN_PROVIDER_UNAVAILABLE"),
        (503, "ONCHAIN_PROVIDER_UNAVAILABLE"),
    ],
)
def test_global_provider_failures_halt_without_retry_or_redirect(status, code):
    response = Response(
        raw=b"sensitive or misleading response body",
        status=status,
        headers={"Retry-After": "19", "Location": "https://untrusted.invalid"},
    )
    h = Harness({"token": response})
    result = h.collect()
    assert len(h.calls) == 1
    assert result["halt_reason"] == code
    assert reason(result) == reason(result, "pools") == code
    assert result["components"]["token"]["payload"] is None
    assert response.read_sizes == []
    assert "sensitive" not in canonical(result)
    assert result["retry_after_seconds"] > 0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("17", 17),
        ("0", 6),
        ("9999999999", 86400),
        ("NaN", 60),
        ("-20", 60),
        ("\r\nunsafe", 60),
        ("9" * 200, 60),
    ],
)
def test_retry_after_integer_is_bounded(raw, expected):
    result = Harness(
        {"token": Response(status=429, headers={"Retry-After": raw})}
    ).collect()
    assert result["retry_after_seconds"] == expected


def test_retry_after_http_date_is_supported():
    from email.utils import formatdate

    raw = formatdate(NOW / 1000 + 81, usegmt=True)
    result = Harness(
        {"token": Response(status=503, headers={"Retry-After": raw})}
    ).collect()
    assert result["retry_after_seconds"] == 81


def test_nonfatal_token_missing_still_collects_pool_component():
    h = Harness({"token": Response(status=404, raw=b"missing")})
    result = h.collect()
    assert reason(result) == "ONCHAIN_PUBLIC_HTTP_ERROR"
    assert result["components"]["pools"]["status"] == "OBSERVED"
    assert result["halt_reason"] is None
    assert len(h.calls) == 2


@pytest.mark.parametrize("declared", [False, True])
def test_response_size_limit_does_not_read_unbounded_body(declared):
    response = Response(
        raw=b"x" * (CAP + 2),
        headers={"Content-Length": str(CAP + 2)} if declared else {},
    )
    result = Harness({"token": response}).collect()
    assert reason(result) == "ONCHAIN_RESPONSE_TOO_LARGE"
    assert response.read_sizes == ([] if declared else [CAP + 1])


@pytest.mark.parametrize(
    "raw", [b"not json", b'{"v":NaN}', b'{"id":"a","id":"b"}', b"\xff", b"[" * 1500]
)
def test_malformed_json_is_unknown_and_never_echoed(raw):
    result = Harness({"token": Response(raw=raw)}).collect()
    assert reason(result) == "ONCHAIN_INVALID_JSON"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(id=OTHER),
        lambda value: value.update(chain="ethereum"),
        lambda value: value["summary"].update(id=OTHER),
        lambda value: value["summary"].update(chain="ethereum"),
        lambda value: value["summary"].update(liquidity_usd="NaN"),
        lambda value: value["summary"].update(liquidity_usd=-1),
        lambda value: value["summary"].update(price_usd="1e101"),
        lambda value: value["summary"].update(price_usd="1e1000000"),
        lambda value: value["summary"].update(price_usd="1e-101"),
        lambda value: value["summary"]["5m"].update(buys=True),
        lambda value: value["summary"]["5m"].update(sells=-1),
        lambda value: value.update(decimals=256),
        lambda value: value.update(no_index="false"),
    ],
)
def test_token_invalid_identity_or_numerics_are_not_evidence(mutate):
    value = token()
    mutate(value)
    result = Harness({"token": Response(value)}).collect()
    assert reason(result) == "ONCHAIN_RESPONSE_SHAPE"
    assert result["components"]["token"]["payload"] is None
    assert result["components"]["pools"]["status"] == "OBSERVED"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("query"),
        lambda value: value["query"].pop("token_address"),
        lambda value: value["query"].update(network="ethereum"),
        lambda value: value["query"].update(token_address=OTHER),
        lambda value: value["query"].update(limit=10),
        lambda value: value["query"].update(sort="asc"),
        lambda value: value["results"][0].update(chain="ethereum"),
        lambda value: value["results"][0]["tokens"][0].update(id=OTHER),
        lambda value: value["results"].append(copy.deepcopy(value["results"][0])),
        lambda value: value.update(has_next_page="false"),
    ],
)
def test_pool_echo_and_membership_must_match(mutate):
    value = pools()
    mutate(value)
    result = Harness({"pools": Response(value)}).collect()
    assert reason(result, "pools") == "ONCHAIN_RESPONSE_SHAPE"
    assert result["components"]["token"]["status"] == "OBSERVED"


def test_empty_pool_results_are_recorded_without_fabricating_liquidity():
    value = pools()
    value.update(results=[], has_next_page=False)
    result = Harness({"pools": Response(value)}).collect()
    assert result["components"]["pools"]["status"] == "OBSERVED"
    assert result["components"]["pools"]["payload"]["results"] == []


def test_missing_nested_summary_identity_is_not_fabricated():
    value = token()
    value["summary"].pop("id")
    value["summary"].pop("chain")
    result = Harness({"token": Response(value)}).collect()
    summary = result["components"]["token"]["payload"]["summary"]
    assert "id" not in summary
    assert "chain" not in summary


def test_all_symbols_share_six_request_budget_and_six_second_spacing():
    h = Harness()
    results = [h.collect() for _ in range(4)]
    assert h.market.requests == len(h.calls) == 6
    assert [call[0] for call in h.calls] == [0, 6, 12, 18, 24, 30]
    assert results[-1]["halt_reason"] == "ONCHAIN_REQUEST_LIMIT"


def test_deadline_reserves_whole_request_without_sleeping_past_cap():
    h = Harness(max_seconds=7)
    result = h.collect()
    assert len(h.calls) == 1
    assert h.now == 0
    assert result["halt_reason"] == "ONCHAIN_DEADLINE"


def test_response_after_global_deadline_is_discarded():
    h = Harness()

    def late():
        h.now = 51
        return Response(token())

    h.overrides["token"] = late
    result = h.collect()
    assert result["halt_reason"] == "ONCHAIN_DEADLINE"
    assert reason(result) == "ONCHAIN_DEADLINE"
    assert len(h.calls) == 1


@pytest.mark.parametrize(
    "error,code",
    [
        (TimeoutError("do not leak"), "ONCHAIN_REQUEST_TIMEOUT"),
        (OSError("do not leak"), "ONCHAIN_TRANSPORT_FAILURE"),
    ],
)
def test_transport_failure_halts_batch_and_hides_details(error, code):
    h = Harness({"token": error})
    result = h.collect()
    assert result["halt_reason"] == code
    assert len(h.calls) == 1
    assert "do not leak" not in canonical(result)


def test_hard_timeout_covers_blocked_dns_or_headers_not_just_socket_reads(monkeypatch):
    monkeypatch.setattr(module, "TIMEOUT", 0.03)
    blocked = threading.Event()
    released = threading.Event()
    h = Harness()

    def hanging():
        blocked.set()
        released.wait(2)
        return Response(token())

    h.overrides["token"] = hanging
    start = time.monotonic()
    try:
        result = h.collect()
        elapsed = time.monotonic() - start
        assert blocked.is_set()
        assert elapsed < 0.5
        assert result["halt_reason"] == "ONCHAIN_REQUEST_TIMEOUT"
        assert len(h.calls) == 1
    finally:
        released.set()


def test_worker_start_failure_is_unknown_and_halts(monkeypatch):
    def fail_start(_):
        raise RuntimeError("system resource detail must not leak")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    h = Harness()
    result = h.collect()
    assert result["halt_reason"] == "ONCHAIN_TRANSPORT_FAILURE"
    assert not h.calls
    assert "resource detail" not in canonical(result)


def test_stale_call_clock_rejected_before_http():
    h = Harness()
    h.now = 51
    with pytest.raises(ValueError, match="ONCHAIN_CURRENT_CLOCK_REQUIRED"):
        h.collect()
    assert not h.calls
