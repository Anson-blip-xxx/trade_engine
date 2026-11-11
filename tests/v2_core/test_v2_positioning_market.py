import json
from urllib.parse import parse_qs, urlsplit

import pytest

from services.v2_positioning_market import CAP, HOST, PositioningMarket
from v2_core.positioning_evidence import ENDPOINTS, HOUR, STEP, describe

NOW = 20_000 * 24 * HOUR + 12 * HOUR + 120_000


class Response:
    def __init__(self, value=None, *, status=200, raw=None, retry="60"):
        self.status, self.retry = status, retry
        self.raw = json.dumps(value).encode() if raw is None else raw

    def read(self, limit):
        assert limit == CAP + 1
        return self.raw[:limit]

    def getheader(self, name, default):
        assert name == "Retry-After"
        return self.retry


class Harness:
    def __init__(self, overrides=None, **limits):
        self.now = 0
        self.calls, self.weights, self.penalties, self.closed = [], [], [], []
        self.allow, self.quota_error = True, False
        self.overrides = overrides or {}
        self.market = PositioningMarket(
            self,
            factory=self.factory,
            clock_ms=lambda: NOW + int(self.now * 1000),
            monotonic=lambda: self.now,
            sleep=self.sleep,
            **limits,
        )

    def sleep(self, seconds):
        self.now += seconds

    def permit(self, weight):
        self.weights.append(weight)
        if self.quota_error:
            raise RuntimeError("private quota detail must not leak")
        return self.allow

    def penalize(self, seconds):
        self.penalties.append(seconds)

    def factory(self, host, *, timeout):
        assert host == HOST and timeout == 5
        harness = self

        class Connection:
            def request(self, method, path):
                assert method == "GET"
                url = urlsplit(path)
                self.path, self.params = url.path, parse_qs(url.query)
                harness.calls.append((host, method, self.path, self.params))

            def getresponse(self):
                value = harness.overrides.get(self.path)
                if isinstance(value, Exception):
                    raise value
                if value:
                    return value
                if self.path == ENDPOINTS["premium_index"]:
                    return Response(
                        {
                            "symbol": "TESTUSDT",
                            "time": NOW,
                            "markPrice": "100",
                            "indexPrice": "100",
                            "lastFundingRate": "0",
                            "nextFundingTime": NOW + HOUR,
                        }
                    )
                return Response([{}])  # shape accepted; semantics belong to describe

            def close(self):
                harness.closed.append(self.path)

        return Connection()

    def collect(self):
        return self.market.collect("TESTUSDT", now_ms=NOW)


def test_exact_seven_public_gets_and_shared_weight_for_every_attempt():
    h = Harness()
    b = h.collect()
    assert len(h.calls) == len(h.weights) == len(h.closed) == 7
    assert h.weights == [1] * 7
    assert [x[2] for x in h.calls] == list(ENDPOINTS.values())
    assert all(x[0] == "fapi.binance.com" for x in h.calls)
    p = h.calls[0][3]
    end = NOW // HOUR * HOUR
    assert p == {
        "symbol": ["TESTUSDT"],
        "interval": ["1h"],
        "startTime": [str(end - 72 * HOUR)],
        "endTime": [str(end - 1)],
        "limit": ["72"],
    }
    assert h.calls[1][3]["period"] == ["5m"] and h.calls[1][3]["limit"] == ["80"]
    assert h.calls[1][3]["startTime"] == [str(NOW // STEP * STEP - 6 * HOUR - 2 * STEP)]
    assert h.calls[5][3]["startTime"] == [str(NOW - 24 * HOUR)]
    assert all(isinstance(c["rows"], list) for c in b["components"].values())
    assert b["halt_reason"] is None
    assert not any(
        b[k]
        for k in (
            "execution_authorized",
            "automatic_parameter_updates",
            "promotion_authorized",
        )
    )


@pytest.mark.parametrize("status", [301, 302, 401, 403, 404, 500])
def test_http_failure_is_component_local_no_redirect_or_credentials(status):
    h = Harness({ENDPOINTS["open_interest"]: Response(status=status)})
    b = h.collect()
    assert len(h.calls) == 7
    assert b["components"]["open_interest"]["reason"] == "POSITIONING_PUBLIC_HTTP_ERROR"
    assert b["components"]["premium_index"]["status"] == "OBSERVED"
    assert h.market.halt_reason is None


@pytest.mark.parametrize(
    "status,retry,expected",
    [(429, "120", 120), (418, "invalid", 60), (429, "99999999", 259200)],
)
def test_ban_penalizes_shared_budget_and_latches_across_symbols(
    status, retry, expected
):
    h = Harness({ENDPOINTS["open_interest"]: Response(status=status, retry=retry)})
    b = h.collect()
    assert len(h.calls) == 2 and h.penalties == [expected]
    assert b["components"]["hourly_klines"]["status"] == "OBSERVED"
    assert b["components"]["premium_index"]["reason"] == "POSITIONING_RATE_LIMITED"
    h.collect()
    assert len(h.calls) == 2


@pytest.mark.parametrize("error", [False, True])
def test_quota_failure_stops_instance_without_network(error):
    h = Harness()
    h.allow, h.quota_error = False, error
    b = h.collect()
    assert not h.calls and len(h.weights) == 1
    expected = "POSITIONING_QUOTA_FAILURE" if error else "POSITIONING_QUOTA_WAIT"
    assert h.market.halt_reason == expected
    assert all(c["reason"] == expected for c in b["components"].values())


def test_request_cap_is_shared_across_calls():
    h = Harness(max_requests=8)
    h.collect()
    b = h.collect()
    assert len(h.calls) == 8
    assert b["components"]["hourly_klines"]["status"] == "OBSERVED"
    assert b["halt_reason"] == "POSITIONING_REQUEST_LIMIT"


def test_deadline_stops_before_new_request_timeout_could_exceed_budget():
    h = Harness(max_seconds=30)
    h.now = 27
    b = h.collect()
    assert not h.calls and b["halt_reason"] == "POSITIONING_DEADLINE"


def test_slow_quota_check_cannot_start_network_after_deadline():
    h = Harness(max_seconds=30)

    def permit(weight):
        h.weights.append(weight)
        h.now = 31
        return True

    h.permit = permit
    b = h.collect()
    assert not h.calls and b["halt_reason"] == "POSITIONING_DEADLINE"


def test_penalty_failure_does_not_resume_banned_instance():
    h = Harness({ENDPOINTS["open_interest"]: Response(status=429)})

    def fail(_):
        raise RuntimeError("sensitive penalty error")

    h.penalize = fail
    b = h.collect()
    assert len(h.calls) == 2 and b["halt_reason"] == "POSITIONING_RATE_LIMITED"
    assert "sensitive" not in json.dumps(b)


def test_collector_cannot_be_used_to_fetch_old_arbitrary_days():
    h = Harness()
    with pytest.raises(ValueError, match="CURRENT_CLOCK"):
        h.market.collect("TESTUSDT", now_ms=NOW - 24 * HOUR)
    assert not h.calls


@pytest.mark.parametrize(
    "response,reason",
    [
        (Response(raw=b"x" * (CAP + 1)), "POSITIONING_RESPONSE_TOO_LARGE"),
        (Response(raw=b"{broken"), "POSITIONING_PUBLIC_TRANSPORT_FAILURE"),
        (Response([]), "POSITIONING_RESPONSE_SHAPE"),
        (Response({"code": -1, "msg": "untrusted"}), "POSITIONING_PUBLIC_API_ERROR"),
        (
            RuntimeError("token-sensitive-details"),
            "POSITIONING_PUBLIC_TRANSPORT_FAILURE",
        ),
    ],
)
def test_malformed_or_transport_failure_does_not_abort_other_components(
    response, reason
):
    h = Harness({ENDPOINTS["open_interest"]: response})
    b = h.collect()
    assert b["components"]["open_interest"]["reason"] == reason
    assert len(h.calls) == 7
    assert "sensitive" not in json.dumps(b) and "untrusted" not in json.dumps(b)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_seconds": 29},
        {"max_seconds": 181},
        {"max_requests": 0},
        {"max_requests": 101},
        {"request_spacing": 0},
        {"max_requests": True},
    ],
)
def test_bounds_rejected(limits):
    with pytest.raises(ValueError):
        Harness(**limits)


@pytest.mark.parametrize(
    "target", ["BTC/USDT", "https://host/USDT", "BTCUSDT?x=1", "BTCUSDT\n"]
)
def test_invalid_symbol_never_reaches_network(target):
    h = Harness()
    with pytest.raises(ValueError):
        h.market.collect(target, now_ms=NOW)
    assert not h.calls


def test_observation_semantics_are_validated_by_describe_without_network():
    h = Harness()
    b = h.collect()
    r = describe(b, cutoff_ms=b["finished_at_ms"], as_of_ms=b["finished_at_ms"])
    assert r["status"] == "PARTIAL"
    assert r["components"]["premium_index"]["status"] == "OBSERVED"
    assert r["components"]["open_interest"]["status"] == "UNKNOWN"
    assert len(h.calls) == 7
