import json
from types import SimpleNamespace

import pytest
from market_fakes import ArchiveClient, Budget, MarketHTTP, candle_batch

from services.v2_market_pipeline import BinanceCandleCollector, MarketSupervisor
from v2_core.chunked_archive import ClickHouseChunkedArchive
from v2_core.evidence import canonical, digest
from v2_core.market_archive import ArchiveIntegrityError, ClickHouseCandleArchive
from v2_core.public_market import BinancePublicMarket, PublicMarketError


def test_hour_blocks_reuse_overlap_and_reconstruct_exact_original():
    client = ArchiveClient()
    archive = ClickHouseChunkedArchive(client)
    first = canonical(candle_batch())
    assert archive.put(digest(first), first)
    assert len(client.tables["v2_candle_archive"]) == 24
    second = canonical(candle_batch(60000))
    assert archive.put(digest(second), second)
    assert len(client.tables["v2_candle_archive"]) == 26
    assert archive.get(digest(first)) == first and archive.get(digest(second)) == second
    inserts = len(
        [entry for entry in client.inserts if entry[0] == "v2_candle_archive"]
    )
    assert archive.put(digest(second), second)
    assert (
        len([entry for entry in client.inserts if entry[0] == "v2_candle_archive"])
        == inserts
    )


@pytest.mark.parametrize(
    "defect", ["block", "manifest", "missing", "wrong_reconstruction"]
)
def test_chunk_integrity_and_missing_replica_visibility(defect):
    client = ArchiveClient()
    archive = ClickHouseChunkedArchive(client)
    encoded = canonical(candle_batch())
    key = digest(encoded)
    assert archive.put(key, encoded)
    block = next(iter(client.tables["v2_candle_archive"]))
    if defect == "block":
        client.tables["v2_candle_archive"][block] = ("bad",)
    elif defect == "manifest":
        client.tables["v2_candle_manifests"][key] = ("bad", "a" * 64)
    elif defect == "missing":
        del client.tables["v2_candle_archive"][block]
        assert archive.get(key) is None
        return
    else:
        manifest = json.loads(client.tables["v2_candle_manifests"][key][0])
        manifest["header"]["closed_at"] += 60000
        payload = canonical(manifest)
        client.tables["v2_candle_manifests"][key] = (payload, digest(payload))
    with pytest.raises(ArchiveIntegrityError):
        archive.get(key)


def test_chunk_archive_preserves_old_full_batch_receipts():
    client = ArchiveClient()
    encoded = canonical(candle_batch())
    assert ClickHouseCandleArchive(client).put(digest(encoded), encoded)
    assert ClickHouseChunkedArchive(client).get(digest(encoded)) == encoded


def public(http=None, budget=None, **options):
    return BinancePublicMarket(
        environment="SANDBOX",
        budget=budget or Budget(),
        enabled=True,
        connection_factory=http or MarketHTTP(),
        **options,
    )


@pytest.mark.parametrize(
    "path,params",
    [
        ("/fapi/v1/order", {}),
        ("https://evil.invalid", {}),
        ("/fapi/v1/time", {"apiKey": "forbidden"}),
        ("/fapi/v1/klines", {"symbol": "BTCUSDT"}),
    ],
)
def test_public_client_has_no_arbitrary_endpoint_or_credential_surface(path, params):
    http = MarketHTTP()
    with pytest.raises(PublicMarketError):
        public(http)(path, params)
    assert not http.calls


def test_public_disabled_and_quota_denied_before_network():
    http = MarketHTTP()
    client = BinancePublicMarket(
        environment="SANDBOX", budget=Budget(), connection_factory=http
    )
    with pytest.raises(PublicMarketError, match="DISABLED"):
        client("/fapi/v1/time", {})
    with pytest.raises(PublicMarketError, match="QUOTA_DENIED"):
        public(http, Budget(False))("/fapi/v1/time", {})
    assert not http.calls


@pytest.mark.parametrize(
    "path,params",
    [
        ("/fapi/v1/exchangeInfo", {}),
        (
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": "BTCUSDT", "period": "1h", "limit": 3},
        ),
    ],
)
def test_account_context_public_routes_are_exact_get_only(path, params):
    http, budget = MarketHTTP(raw=b"{}" if not params else b"[]"), Budget()
    public(http, budget)(path, params)
    assert len(http.calls) == 1 and budget.weights == [1]
    with pytest.raises(PublicMarketError, match="ENDPOINT_DISABLED"):
        public(MarketHTTP())(path, {**params, "extra": "forbidden"})


@pytest.mark.parametrize(
    "status,retry,delay",
    [(429, "120", 120), (429, "bad", 60), (418, "", 86400), (418, "99999999", 259200)],
)
def test_rate_limit_persists_backoff_without_retry(status, retry, delay):
    http, budget = MarketHTTP(status=status, retry=retry), Budget()
    with pytest.raises(PublicMarketError, match="RATE_LIMITED"):
        public(http, budget)("/fapi/v1/time", {})
    assert budget.penalties == [delay] and len(http.calls) == http.closed == 1


@pytest.mark.parametrize(
    "raw",
    [
        b'{"serverTime":1,"serverTime":2}',
        b'{"x":NaN}',
        b"not JSON",
        b"1",
        b'{"code":-1,"msg":"SECRET"}',
        b" " * 2000001,
    ],
)
def test_bad_public_responses_are_bounded_and_sanitized(raw):
    http = MarketHTTP(raw=raw)
    with pytest.raises(PublicMarketError) as error:
        public(http)("/fapi/v1/time", {})
    assert "SECRET" not in str(error.value) and http.closed == 1


@pytest.mark.parametrize("status", [301, 403, 500, 503])
def test_public_http_no_redirect_or_automatic_retry(status):
    http = MarketHTTP(status=status)
    with pytest.raises(PublicMarketError):
        public(http)("/fapi/v1/time", {})
    assert len(http.calls) == 1 and http.closed == 1


def collector(http=None, **options):
    return BinanceCandleCollector(
        public(http),
        symbols=["BTCUSDT"],
        clock_ms=lambda: 86402001,
        monotonic_ms=lambda: 0,
        **options,
    )


def test_collector_closed_boundary_fields_and_weight():
    http, budget = MarketHTTP(), Budget()
    service = BinanceCandleCollector(
        public(http, budget),
        symbols=["BTCUSDT"],
        clock_ms=lambda: 86402001,
        monotonic_ms=lambda: 0,
    )
    result = service.collect()
    assert result["closed_at"] == 86400000
    assert result["candles"]["BTCUSDT"][0]["t"] == 86340000
    assert result["candles"]["BTCUSDT"][0]["tbv"] == "60"
    assert budget.weights == [1, 10]
    assert all(
        c[0] == "demo-fapi.binance.com" and c[2] == "GET" and "X-MBX-APIKEY" not in c[4]
        for c in http.calls
    )
    assert service.collect(skip_frame=lambda _: True) is None
    assert budget.weights == [1, 10, 1]


@pytest.mark.parametrize(
    "defect", ["short", "reverse", "duplicate", "close_time", "float", "tuple_size"]
)
def test_collector_rejects_partial_or_inconsistent_exchange_history(defect):
    http = MarketHTTP()

    def mutate(rows):
        if defect == "short":
            return rows[:-1]
        if defect == "reverse":
            return list(reversed(rows))
        if defect == "duplicate":
            rows[1] = rows[0]
        elif defect == "close_time":
            rows[0][6] += 1
        elif defect == "float":
            rows[0][1] = 100.0
        else:
            rows[0].pop()
        return rows

    http.mutate = mutate
    with pytest.raises(ValueError):
        collector(http).collect()


def test_collector_clock_and_total_deadline_fail_closed():
    with pytest.raises(ValueError, match="skew"):
        collector(MarketHTTP(server_time=86500000)).collect()
    service = collector()
    ticks = iter([0, 0, 30000])
    service.monotonic_ms = lambda: next(ticks)
    with pytest.raises(TimeoutError):
        service.collect()


def test_supervisor_recovery_precedes_collection_and_reports_alert_failure():
    publisher = SimpleNamespace(environment="SANDBOX")
    source = SimpleNamespace(
        publisher=publisher, has_pending=lambda: False, has_frame=lambda _: False
    )
    runtime = SimpleNamespace(processor=SimpleNamespace(publisher=publisher))
    service = MarketSupervisor(
        source=source,
        runtime=runtime,
        collector=SimpleNamespace(
            environment="SANDBOX",
            collect=lambda **kwargs: pytest.fail("must recover first"),
        ),
        notify=lambda _: False,
    )
    service.runner = SimpleNamespace(
        run_once=lambda: {
            "status": "RETRY",
            "stage": "READ",
            "error_code": "ConnectionError",
        }
    )
    assert service.run_once()["alert_status"] == "UNAVAILABLE"
    service.runner.run_once = lambda: {"status": "IDLE"}
    source.has_pending = lambda: True
    assert service.run_once() == {"status": "WAITING_SOURCE"}


def test_supervisor_stop_does_not_launch_work():
    service = object.__new__(MarketSupervisor)
    service.run_once = lambda: pytest.fail("stopped loop ran")
    service.serve(
        SimpleNamespace(is_set=lambda: True, wait=lambda _: pytest.fail("waited"))
    )


def test_chunk_read_transport_decode_error_does_not_become_corruption():
    client = ArchiveClient()
    archive = ClickHouseChunkedArchive(client)
    encoded = canonical(candle_batch())
    assert archive.put(digest(encoded), encoded)
    original = client.query

    def unavailable(sql, parameters):
        if "keys" in parameters:
            raise ValueError("transport decode error")
        return original(sql, parameters)

    client.query = unavailable
    with pytest.raises(ValueError) as error:
        archive.get(digest(encoded))
    assert not isinstance(error.value, ArchiveIntegrityError)


def test_non_ascii_retry_header_still_uses_conservative_cooldown():
    http, budget = MarketHTTP(status=429, retry="²"), Budget()
    with pytest.raises(PublicMarketError, match="RATE_LIMITED"):
        public(http, budget)("/fapi/v1/time", {})
    assert budget.penalties == [60]


def test_http_timeout_is_sanitized_and_connection_closed():
    http = MarketHTTP(fail=TimeoutError("secret endpoint"))
    with pytest.raises(PublicMarketError, match="PUBLIC_TRANSPORT_FAILURE") as error:
        public(http)("/fapi/v1/time", {})
    assert (
        "secret" not in str(error.value) and http.closed == 1 and len(http.calls) == 1
    )


def test_skipped_frame_still_obeys_time_request_deadline():
    service = collector()
    ticks = iter([0, 30000])
    service.monotonic_ms = lambda: next(ticks)
    with pytest.raises(TimeoutError):
        service.collect(skip_frame=lambda _: True)


@pytest.mark.parametrize(
    "symbols", [[], ["BTCUSDT", "BTCUSDT"], ["../bad"], ["BTCUSDT"] * 21]
)
def test_collector_universe_validation(symbols):
    with pytest.raises(ValueError):
        BinanceCandleCollector(
            public(), symbols=symbols, clock_ms=lambda: 0, monotonic_ms=lambda: 0
        )


def test_supervisor_stop_waits_between_iterations_not_busy_retry():
    service = object.__new__(MarketSupervisor)
    calls = []
    service.run_once = lambda: calls.append("run")
    stop = SimpleNamespace(
        is_set=lambda: len(calls) >= 2, wait=lambda seconds: calls.append(seconds)
    )
    service.serve(stop, interval_seconds=10)
    assert calls == ["run", 10]
