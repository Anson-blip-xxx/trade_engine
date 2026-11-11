"""Offline public HTTP -> evidence -> job/store-boundary contract tests.

The HTTP fixture constructs only observations actually requested by the market
adapter. It intentionally does not repair stale/partial windows on its behalf.
"""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from services.v2_positioning_entry import GIB, run_batch
from services.v2_positioning_market import CAP, HOST, PositioningMarket
from v2_core.positioning_evidence import ENDPOINTS, HOUR, STEP

EPOCH = 20_000 * 24 * HOUR + 12 * HOUR


class Store:
    def __init__(self, symbols):
        self.symbols = symbols
        self.saved, self.runs, self.pruned = [], [], []
        self.fail_symbol = None

    def prune(self, now_ms, *, limit):
        self.pruned.append((now_ms, limit))
        return 0

    def select_symbols(self, now_ms, *, limit):
        return [
            {"symbol": symbol, "membership": ["FIXED_CONTROL"]}
            for symbol in self.symbols[:limit]
        ]

    def save(self, bundle, *, slot_ms, membership, features):
        if bundle["symbol"] == self.fail_symbol:
            raise RuntimeError("private storage diagnostic")
        assert slot_ms == bundle["started_at_ms"] // STEP * STEP
        assert features["cutoff_ms"] == features["as_of_ms"] == bundle["finished_at_ms"]
        assert membership == ["FIXED_CONTROL"]
        assert features["execution_authorized"] is False
        assert features["automatic_parameter_updates"] is False
        assert features["promotion_authorized"] is False
        self.saved.append((bundle, slot_ms, features))
        return {"inserted": True}

    def record_run(self, **run):
        self.runs.append(run)


class PublicFixture:
    def __init__(
        self,
        *,
        now_ms=EPOCH + 120_000,
        overrides=None,
        latency=0,
        statistics_lag=0,
        **limits,
    ):
        self.epoch, self.elapsed, self.latency = now_ms, 0, latency
        self.statistics_lag = statistics_lag
        self.overrides = overrides or {}
        self.calls, self.weights, self.penalties = [], [], []
        self.market = PositioningMarket(
            self,
            factory=self.factory,
            clock_ms=self.clock,
            monotonic=lambda: self.elapsed,
            sleep=self.sleep,
            **limits,
        )

    def clock(self):
        return self.epoch + round(self.elapsed * 1000)

    def sleep(self, seconds):
        self.elapsed += seconds

    def permit(self, weight):
        self.weights.append(weight)
        return True

    def penalize(self, seconds):
        self.penalties.append(seconds)

    def rows(self, path, query):
        symbol = query["symbol"][0]
        if path == ENDPOINTS["premium_index"]:
            return {
                "symbol": symbol,
                "time": self.clock(),
                "markPrice": "100",
                "indexPrice": "100",
                "lastFundingRate": "0",
                "nextFundingTime": self.clock() + HOUR,
            }
        start, end = int(query["startTime"][0]), int(query["endTime"][0])
        if path == ENDPOINTS["hourly_klines"]:
            return [
                [
                    t,
                    "100",
                    "101",
                    "99",
                    "100",
                    "10",
                    t + HOUR - 1,
                    "1000",
                    2,
                    "5",
                    "500",
                ]
                for t in range(start, end, HOUR)
            ]
        if path == ENDPOINTS["funding_history"]:
            return [
                {"symbol": symbol, "fundingTime": t, "fundingRate": "0"}
                for t in range((start // HOUR + 1) * HOUR, end + 1, 8 * HOUR)
            ]
        return [
            dict(
                symbol=symbol,
                timestamp=t,
                **(
                    {"sumOpenInterest": "100", "sumOpenInterestValue": "10000"}
                    if path == ENDPOINTS["open_interest"]
                    else {
                        "longAccount": "0.6",
                        "shortAccount": "0.4",
                        "longShortRatio": "1.5",
                    }
                ),
            )
            for t in range(start, end - self.statistics_lag * STEP + 1, STEP)
        ]

    def factory(self, host, *, timeout):
        assert host == HOST and timeout == 5
        fixture = self

        class Connection:
            def request(self, method, path):
                assert method == "GET"
                url = urlsplit(path)
                self.path, self.query = url.path, parse_qs(url.query)
                assert self.path in ENDPOINTS.values()
                assert set(self.query) <= {
                    "symbol",
                    "interval",
                    "period",
                    "startTime",
                    "endTime",
                    "limit",
                }
                fixture.calls.append((self.query["symbol"][0], self.path, self.query))

            def getresponse(self):
                fixture.sleep(fixture.latency)
                status = fixture.overrides.get(
                    (self.query["symbol"][0], self.path), 200
                )
                raw = json.dumps(fixture.rows(self.path, self.query)).encode()

                class Response:
                    def read(self, limit):
                        assert limit == CAP + 1
                        return raw[:limit]

                    def getheader(self, name, default):
                        assert name == "Retry-After"
                        return "60"

                response = Response()
                response.status = status
                return response

            def close(self):
                pass

        return Connection()

    def run(self, store, *, batch_symbols=8):
        return run_batch(
            store,
            self.market,
            now_ms=self.epoch,
            clock_ms=self.clock,
            free_disk_bytes=20 * GIB,
            batch_symbols=batch_symbols,
        )


def test_default_eight_symbol_batch_has_56_shared_permits_and_fresh_receipts():
    h = PublicFixture()
    store = Store([f"COIN{i}USDT" for i in range(8)])
    report = h.run(store)
    assert report["status"] == "COMPLETE"
    assert report["inserted"] == 8 and report["failed"] == 0
    assert len(h.calls) == h.market.requests == 56
    assert h.weights == [1] * 56 and h.market.halt_reason is None
    assert report["observed_components"] == dict.fromkeys(ENDPOINTS, 8)
    assert not report["unknown_components"]
    assert store.runs[0]["started_at_ms"] == h.epoch
    assert store.runs[0]["completed_at_ms"] == h.clock()
    for bundle, _, features in store.saved:
        for component in features["components"].values():
            assert component["availability"] == "TIMELY_FIRST_SEEN"
            assert component["historical_receipt_proven"] is False
            assert (
                component["event_ms"]
                <= component["first_seen_ms"]
                <= bundle["finished_at_ms"]
            )
            assert (
                component["first_seen_after_event_ms"]
                == component["first_seen_ms"] - component["event_ms"]
            )


def test_request_limit_is_not_reset_for_next_symbol_and_partial_bundle_is_saved():
    h = PublicFixture(max_requests=8)
    store = Store(["AAAUSDT", "BBBUSDT", "CCCUSDT"])
    report = h.run(store)
    assert report["status"] == "DEFERRED"
    assert (
        report["selected"],
        report["attempted"],
        report["inserted"],
        report["deferred_symbols"],
    ) == (3, 2, 2, 1)
    assert len(h.calls) == len(h.weights) == 8
    assert report["failed"] == 0
    assert h.market.halt_reason == "POSITIONING_REQUEST_LIMIT"
    assert store.saved[1][2]["status"] == "PARTIAL"
    assert store.saved[1][2]["components"]["hourly_klines"]["status"] == "OBSERVED"
    assert store.saved[1][2]["components"]["open_interest"]["status"] == "UNKNOWN"


@pytest.mark.parametrize("status", [418, 429])
def test_shared_ban_stops_batch_but_persists_first_component_evidence(status):
    h = PublicFixture(overrides={("AAAUSDT", ENDPOINTS["open_interest"]): status})
    store = Store(["AAAUSDT", "BBBUSDT"])
    report = h.run(store)
    assert report["status"] == "DEFERRED" and report["deferred_symbols"] == 1
    assert len(h.calls) == 2 and h.penalties == [60]
    assert report["inserted"] == 1 and report["failed"] == 0
    assert store.saved[0][2]["components"]["hourly_klines"]["status"] == "OBSERVED"


def test_ordinary_component_failure_does_not_stop_next_symbol():
    h = PublicFixture(overrides={("AAAUSDT", ENDPOINTS["open_interest"]): 403})
    store = Store(["AAAUSDT", "BBBUSDT"])
    report = h.run(store)
    assert report["status"] == "PARTIAL"
    assert report["unknown_components"] == {"open_interest": 1}
    assert report["inserted"] == 2 and len(h.calls) == 14
    assert report["failed"] == 0
    assert store.saved[1][2]["status"] == "OBSERVED"


def test_deadline_is_shared_across_symbols_and_does_not_start_late_request():
    h = PublicFixture(max_seconds=30, latency=4)
    report = h.run(Store(["AAAUSDT", "BBBUSDT", "CCCUSDT"]))
    assert h.market.halt_reason == "POSITIONING_DEADLINE"
    assert len(h.calls) == 7 and h.elapsed == 28
    assert report["status"] == "DEFERRED" and report["deferred_symbols"] == 1
    assert report["inserted"] == 2
    assert report["failed"] == 0


@pytest.mark.parametrize("statistics_lag,status", [(1, "COMPLETE"), (2, "PARTIAL")])
def test_publication_delay_preserves_usable_six_hours_without_accepting_stale_data(
    statistics_lag, status
):
    h = PublicFixture(statistics_lag=statistics_lag)
    store = Store(["AAAUSDT"])
    report = h.run(store)
    assert report["status"] == status and report["failed"] == 0
    component = store.saved[0][2]["components"]["open_interest"]
    if statistics_lag == 1:
        assert component["features"]["window_6h"]["status"] == "OBSERVED"
        assert component["features"]["window_6h"]["end_ms"] == EPOCH - STEP
        assert not report["unknown_windows"]
    else:
        assert component["reason"] == "STALE_STATISTICS"


def test_crossing_slot_uses_each_bundle_start_not_batch_or_finish_slot():
    h = PublicFixture(now_ms=EPOCH + STEP - 1000)
    store = Store(["AAAUSDT", "BBBUSDT"])
    report = h.run(store)
    assert report["status"] == "COMPLETE"
    first, second = store.saved
    assert first[1] == EPOCH and second[1] == EPOCH + STEP
    assert first[0]["finished_at_ms"] // STEP * STEP != first[1]
    assert first[2]["cutoff_ms"] < second[2]["cutoff_ms"]


def test_crossing_hour_never_invents_unrequested_closed_candle():
    h = PublicFixture(now_ms=EPOCH + HOUR - 1000)
    store = Store(["AAAUSDT", "BBBUSDT"])
    report = h.run(store)
    assert report["status"] == "PARTIAL"
    assert report["unknown_windows"] == {
        "hourly_klines.window_3h": 2,
        "hourly_klines.window_6h": 2,
    }
    for bundle, _, features in store.saved:
        hourly = features["components"]["hourly_klines"]["features"]
        assert bundle["finished_at_ms"] > EPOCH + HOUR
        assert hourly["available_bars"] == 71
        assert hourly["complete_72h"] is False
        assert hourly["window_3h"]["status"] == "UNKNOWN"
        assert hourly["window_6h"]["status"] == "UNKNOWN"
        assert (
            max(row[6] for row in bundle["components"]["hourly_klines"]["rows"])
            == EPOCH - 1
        )


def test_symbol_storage_failure_cannot_reset_budget_or_discard_peer_evidence():
    h = PublicFixture()
    store = Store(["AAAUSDT", "BBBUSDT"])
    store.fail_symbol = "AAAUSDT"
    report = h.run(store)
    assert report["status"] == "PARTIAL"
    assert report["failed"] == report["inserted"] == 1
    assert len(h.calls) == 14 and store.saved[0][0]["symbol"] == "BBBUSDT"
    assert "private" not in str(report)
