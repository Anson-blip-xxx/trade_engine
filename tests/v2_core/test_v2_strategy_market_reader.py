import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from services.v2_strategy_market_reader import ClosedMarketBars, ClosedMarketReader
from v2_core.evidence import canonical, digest
from v2_core.strategy_candidate_builder import CandidatePolicy, build_candidate

MINUTE = 60000
CLOSED = 1_800_000_000_000


def bar(opened, **changes):
    return {
        "t": opened,
        "o": "10",
        "h": "11",
        "l": "9",
        "c": "10.5",
        "v": "8",
        "tbv": "4",
        **changes,
    }


def row(
    opened,
    *,
    payload=None,
    fingerprint=None,
    target="BTCUSDT",
    environment="SANDBOX",
    **changes,
):
    encoded = (
        json.dumps(bar(opened, **changes), sort_keys=True, separators=(",", ":"))
        if payload is None
        else payload
    )
    return (
        environment,
        target,
        opened,
        encoded,
        digest(encoded) if fingerprint is None else fingerprint,
    )


class Client:
    def __init__(self, rows, error=None, *, settings=None):
        self.rows, self.error, self.calls = rows, error, []
        self.query_limit = 0
        self.query_retries = 0
        self._send_receive_timeout = 10
        profile = {
            "readonly": 1,
            "max_threads": 1,
            "max_execution_time": 5,
            "timeout_overflow_mode": "throw",
            "max_result_rows": 2880,
            "max_result_bytes": 4194304,
            "result_overflow_mode": "throw",
            **(settings or {}),
        }
        self.server_settings = {
            name: SimpleNamespace(value=str(value), readonly=1)
            for name, value in profile.items()
        }

    def get_client_setting(self, key):
        return None

    def _validate_settings(self, settings):
        # Real ClickHouse Connect omits settings matching fixed server values.
        assert all(
            self.server_settings[key].value == str(value)
            for key, value in settings.items()
        )
        return {}

    def query(self, sql, *, parameters, settings):
        self.calls.append((sql, parameters, settings))
        if self.error:
            raise self.error
        return SimpleNamespace(result_rows=self.rows)


def reader(rows, *, times=(CLOSED + 2, CLOSED + 7), **limits):
    client = Client(
        rows,
        settings={
            "max_execution_time": limits.get("query_timeout_seconds", 5),
            "max_result_rows": limits.get("max_raw_rows", 2880),
            "max_result_bytes": limits.get("max_payload_bytes", 4194304),
        },
    )
    clock = iter(times)
    return ClosedMarketReader(client, clock_ms=lambda: next(clock), **limits), client


def test_scoped_single_select_has_enforced_throw_limits_and_detached_oldest_first():
    latest = row(CLOSED - MINUTE)
    first = list(row(CLOSED - 2 * MINUTE))
    first[-1] = first[-1].encode("ascii")
    subject, client = reader([latest, first, latest])
    facts = subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=2)
    assert facts.symbol == "BTCUSDT"
    assert facts.environment == "SANDBOX"
    assert facts.closed_at_ms == CLOSED
    assert facts.available_at_ms == CLOSED + 7
    assert [item["t"] for item in facts.bars] == [CLOSED - 2 * MINUTE, CLOSED - MINUTE]
    assert len(client.calls) == 1
    sql, parameters, settings = client.calls[0]
    assert parameters == {
        "environment": "SANDBOX",
        "symbol": "BTCUSDT",
        "start": CLOSED - 2 * MINUTE,
        "end": CLOSED,
    }
    assert "LIMIT" not in sql and "GROUP BY" not in sql and "DISTINCT" not in sql
    assert "SELECT environment,symbol,open_ms,payload,content_digest" in sql
    assert settings == {
        "readonly": 1,
        "max_threads": 1,
        "max_execution_time": 5,
        "timeout_overflow_mode": "throw",
        "max_result_rows": 2880,
        "max_result_bytes": 4194304,
        "result_overflow_mode": "throw",
    }
    facts.bars[0]["c"] = "999"
    assert facts.bars[0]["c"] == "10.5"
    with pytest.raises(FrozenInstanceError):
        facts.symbol = "ETHUSDT"


def test_configured_limits_reach_server():
    subject, client = reader(
        [row(CLOSED - MINUTE)],
        max_raw_rows=3,
        max_payload_bytes=2048,
        query_timeout_seconds=2,
    )
    subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)
    settings = client.calls[0][2]
    assert (
        settings["max_result_rows"],
        settings["max_result_bytes"],
        settings["max_execution_time"],
    ) == (3, 2048, 2)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_raw_rows": True},
        {"max_raw_rows": 0},
        {"max_raw_rows": 10001},
        {"max_raw_rows": 1.0},
        {"max_payload_bytes": 1023},
        {"max_payload_bytes": 8 * 1024 * 1024 + 1},
        {"max_payload_bytes": "4096"},
        {"query_timeout_seconds": False},
        {"query_timeout_seconds": 0},
        {"query_timeout_seconds": 11},
        {"query_timeout_seconds": 1.0},
    ],
)
def test_constructor_limits_are_strict(kwargs):
    with pytest.raises(ValueError, match="CANDLE_READ_LIMIT_INVALID"):
        ClosedMarketReader(Client([]), clock_ms=lambda: CLOSED, **kwargs)


@pytest.mark.parametrize("client,clock", [(None, lambda: CLOSED), (Client([]), None)])
def test_constructor_requires_read_port_and_clock(client, clock):
    with pytest.raises(TypeError, match="CANDLE_READ_PORT_REQUIRED"):
        ClosedMarketReader(client, clock_ms=clock)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minutes": True},
        {"minutes": 0},
        {"minutes": 1441},
        {"minutes": "1"},
        {"closed_at_ms": CLOSED + 1},
        {"closed_at_ms": CLOSED + MINUTE},
        {"closed_at_ms": 0},
        {"closed_at_ms": True},
        {"closed_at_ms": CLOSED + 0.0},
    ],
)
def test_bad_read_window_fails_before_query(kwargs):
    subject, client = reader([])
    with pytest.raises(ValueError):
        subject.read("BTCUSDT", **{"closed_at_ms": CLOSED, "minutes": 1, **kwargs})
    assert client.calls == []


def test_request_exceeding_row_budget_fails_before_query():
    subject, client = reader([], max_raw_rows=1)
    with pytest.raises(ValueError, match="CANDLE_ARCHIVE_SCOPE"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=2)
    assert client.calls == []


def test_bad_symbol_never_reaches_query():
    subject, client = reader([])
    with pytest.raises(ValueError):
        subject.read("BTCUSDT' OR 1=1", closed_at_ms=CLOSED, minutes=1)
    assert client.calls == []


def test_unicode_contract_is_parameterized_not_rejected():
    subject, client = reader([row(CLOSED - MINUTE, target="牛来USDT")])
    assert subject.read("牛来USDT", closed_at_ms=CLOSED, minutes=1).symbol == "牛来USDT"
    assert client.calls[0][1]["symbol"] == "牛来USDT"
    assert "牛来" not in client.calls[0][0]


@pytest.mark.parametrize(
    "rows",
    [[], [row(CLOSED - MINUTE)], [row(CLOSED - 2 * MINUTE), row(CLOSED - 2 * MINUTE)]],
)
def test_missing_or_duplicate_only_history_is_unknown_not_shortened(rows):
    subject, _ = reader(rows)
    with pytest.raises(ValueError, match="CANDLE_HISTORY_INCOMPLETE"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=2)


@pytest.mark.parametrize(
    "bad",
    [
        row(CLOSED - MINUTE, environment="LIVE"),
        row(CLOSED - MINUTE, target="ETHUSDT"),
        row(CLOSED),
        row(CLOSED - 2 * MINUTE),
        row(CLOSED - MINUTE + 1),
        ("SANDBOX", "BTCUSDT", True, "{}", "0" * 64),
    ],
)
def test_each_raw_row_must_match_exact_query_scope(bad):
    subject, _ = reader([bad])
    with pytest.raises(ValueError, match="CANDLE_ARCHIVE_SCOPE"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)


def test_payload_time_must_match_row_time():
    subject, _ = reader([row(CLOSED - MINUTE, payload=canonical(bar(CLOSED)))])
    with pytest.raises(ValueError, match="CANDLE_ARCHIVE_SCOPE"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)


@pytest.mark.parametrize("fingerprint", ["0" * 64, b"\xff" * 64, 123])
def test_digest_required_for_every_row(fingerprint):
    subject, _ = reader([row(CLOSED - MINUTE, fingerprint=fingerprint)])
    with pytest.raises(ValueError, match="CANDLE_ARCHIVE_CONFLICT"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)


def test_conflicting_duplicate_cannot_hide_behind_deduplication():
    subject, _ = reader([row(CLOSED - MINUTE), row(CLOSED - MINUTE, c="10.6")])
    with pytest.raises(ValueError, match="CANDLE_ARCHIVE_CONFLICT"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)


def test_identical_duplicate_still_requires_valid_digest():
    subject, _ = reader([row(CLOSED - MINUTE), row(CLOSED - MINUTE, fingerprint="bad")])
    with pytest.raises(ValueError, match="CANDLE_ARCHIVE_CONFLICT"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)


def test_raw_row_budget_counts_duplicates():
    subject, _ = reader([row(CLOSED - MINUTE)] * 3, max_raw_rows=2)
    with pytest.raises(ValueError, match="CANDLE_ARCHIVE_ROW_BOUND"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)


def test_payload_budget_counts_duplicate_bytes():
    single = row(CLOSED - MINUTE)
    repeated = [single] * (1024 // len(single[3].encode()) + 1)
    subject, _ = reader(repeated, max_payload_bytes=1024)
    with pytest.raises(ValueError, match="CANDLE_ARCHIVE_PAYLOAD_BOUND"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)


def test_single_raw_row_has_an_independent_byte_bound():
    payload = " " * 16385 + canonical(bar(CLOSED - MINUTE))
    subject, _ = reader([row(CLOSED - MINUTE, payload=payload)])
    with pytest.raises(ValueError, match="CANDLE_ARCHIVE_PAYLOAD_BOUND"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)


@pytest.mark.parametrize(
    "rows,code",
    [
        (iter([]), "CANDLE_ARCHIVE_ROW_BOUND"),
        ([{}], "CANDLE_ARCHIVE_ROW_INVALID"),
        ([tuple(row(CLOSED - MINUTE))[:-1]], "CANDLE_ARCHIVE_ROW_INVALID"),
        (
            [("SANDBOX", "BTCUSDT", CLOSED - MINUTE, b"{}", "x")],
            "CANDLE_ARCHIVE_PAYLOAD_INVALID",
        ),
        ([row(CLOSED - MINUTE, payload="{")], "CANDLE_ARCHIVE_PAYLOAD_INVALID"),
        ([row(CLOSED - MINUTE, payload="[]")], "CANDLE_ARCHIVE_SCOPE"),
    ],
)
def test_bad_response_shape_is_fixed_error(rows, code):
    subject, _ = reader(rows)
    with pytest.raises(ValueError, match=code):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)


def test_duplicate_json_fields_are_rejected_even_with_matching_digest():
    payload = canonical(bar(CLOSED - MINUTE)).replace('"o":"10"', '"o":"10","o":"11"')
    subject, _ = reader([row(CLOSED - MINUTE, payload=payload)])
    with pytest.raises(ValueError, match="CANDLE_ARCHIVE_PAYLOAD_INVALID"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)


@pytest.mark.parametrize(
    "changes",
    [
        {"c": "12"},
        {"o": "8"},
        {"l": "0"},
        {"v": "-1"},
        {"tbv": "-1"},
        {"tbv": "9"},
        {"c": "NaN"},
        {"h": "Infinity"},
        {"h": "1e1000"},
        {"c": "1" * 129},
        {"o": 10},
        {"v": 0.0},
        {"tbv": None},
        {"extra": "not-a-bar-field"},
    ],
)
def test_ohlcv_and_measured_taker_facts_are_validated(changes):
    subject, _ = reader([row(CLOSED - MINUTE, **changes)])
    with pytest.raises(ValueError, match="CANDLE_ARCHIVE_BAR_INVALID"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)


def test_zero_taker_is_observed_zero_not_missing():
    subject, _ = reader([row(CLOSED - MINUTE, tbv="0")])
    assert subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1).bars[0]["tbv"] == "0"


def test_query_exception_is_redacted_and_not_retried():
    client = Client([], RuntimeError("secret=do-not-print"))
    subject = ClosedMarketReader(client, clock_ms=lambda: CLOSED)
    with pytest.raises(ValueError) as caught:
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)
    assert str(caught.value) == "CANDLE_ARCHIVE_READ_FAILED"
    assert len(client.calls) == 1


def test_clock_regression_after_query_is_unknown():
    subject, _ = reader([row(CLOSED - MINUTE)], times=(CLOSED + 10, CLOSED + 9))
    with pytest.raises(ValueError, match="CANDLE_ARCHIVE_CLOCK_REGRESSION"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)


def test_complete_short_window_is_valid_facts_but_builder_warmup_unknown():
    subject, _ = reader([row(CLOSED - MINUTE)])
    facts = subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)
    policy = CandidatePolicy("1", "0.5", "0", "0", "3", "20", 86400000, 60000)
    decision = build_candidate(
        facts.bars,
        symbol=facts.symbol,
        environment=facts.environment,
        closed_at_ms=facts.closed_at_ms,
        available_at_ms=facts.available_at_ms,
        decided_at_ms=CLOSED + 8,
        policy=policy,
    )
    assert (decision.status, decision.reason) == ("UNKNOWN", "STRUCTURE_WARMUP")


def test_maximum_window_is_oldest_first_and_builder_compatible():
    rows = [row(CLOSED - offset * MINUTE) for offset in range(1, 1441)]
    subject, _ = reader(rows)
    facts = subject.read("BTCUSDT", closed_at_ms=CLOSED)
    assert len(facts.bars) == 1440
    assert facts.bars[0]["t"] == CLOSED - 1440 * MINUTE
    assert facts.bars[-1]["t"] == CLOSED - MINUTE


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": "LIVE"},
        {"closed_at_ms": CLOSED + 1},
        {"available_at_ms": CLOSED - 1},
        {"bars_json": "[]"},
        {"bars_json": json.dumps([bar(CLOSED)])},
    ],
)
def test_detached_result_cannot_relabel_scope_or_accept_invalid_history(changes):
    arguments = {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "closed_at_ms": CLOSED,
        "available_at_ms": CLOSED,
        "bars_json": json.dumps([bar(CLOSED - MINUTE)]),
    }
    with pytest.raises(ValueError):
        ClosedMarketBars(**{**arguments, **changes})


def test_result_json_is_canonical_not_mutable_alias():
    value = [bar(CLOSED - MINUTE)]
    facts = ClosedMarketBars(
        "BTCUSDT", "SANDBOX", CLOSED, CLOSED, json.dumps(value, indent=2)
    )
    value[0]["o"] = "11"
    assert facts.bars_json == "[" + canonical(bar(CLOSED - MINUTE)) + "]"


@pytest.mark.parametrize("value", [None, True, False, 1, -1, 1440, "0", 0.0])
def test_client_implicit_limit_is_rejected_at_construction(value):
    client = Client([])
    client.query_limit = value
    with pytest.raises(ValueError, match="CANDLE_READ_CLIENT_LIMIT_FORBIDDEN"):
        ClosedMarketReader(client, clock_ms=lambda: CLOSED)
    assert client.calls == []


def test_missing_client_limit_metadata_is_not_assumed_zero():
    client = Client([])
    del client.query_limit
    with pytest.raises(ValueError, match="CANDLE_READ_CLIENT_LIMIT_FORBIDDEN"):
        ClosedMarketReader(client, clock_ms=lambda: CLOSED)


@pytest.mark.parametrize("value", [None, True, False, 1, 2, "0", 0.0])
def test_client_retries_are_rejected_at_construction(value):
    client = Client([])
    client.query_retries = value
    with pytest.raises(ValueError, match="CANDLE_READ_CLIENT_RETRIES_FORBIDDEN"):
        ClosedMarketReader(client, clock_ms=lambda: CLOSED)
    assert client.calls == []


@pytest.mark.parametrize("value", [None, True, 0, 4, 16, 300, "10", 10.0])
def test_client_receive_timeout_must_be_bounded_and_verified(value):
    client = Client([])
    client._send_receive_timeout = value
    with pytest.raises(ValueError, match="CANDLE_READ_CLIENT_TIMEOUT_UNVERIFIED"):
        ClosedMarketReader(client, clock_ms=lambda: CLOSED)
    assert client.calls == []


@pytest.mark.parametrize(
    "name", ["server_settings", "_validate_settings", "get_client_setting"]
)
def test_missing_client_setting_capability_is_not_assumed_safe(name):
    client = Client([])
    setattr(client, name, None)
    with pytest.raises(ValueError, match="CANDLE_READ_SETTINGS_UNVERIFIED"):
        ClosedMarketReader(client, clock_ms=lambda: CLOSED)
    assert client.calls == []


@pytest.mark.parametrize(
    "definition",
    [
        None,
        {},
        SimpleNamespace(value=5, readonly=1),
        SimpleNamespace(value="5", readonly=True),
        SimpleNamespace(value="5", readonly=2),
        SimpleNamespace(value="0", readonly=1),
        SimpleNamespace(value="3", readonly=1),
        SimpleNamespace(value="3", readonly=0),
    ],
)
def test_missing_malformed_or_different_server_budget_is_rejected(definition):
    client = Client([])
    client.server_settings["max_execution_time"] = definition
    with pytest.raises(ValueError, match="CANDLE_READ_SETTINGS_UNVERIFIED"):
        ClosedMarketReader(client, clock_ms=lambda: CLOSED)


def test_unrestricted_default_account_cannot_be_downgraded_by_this_reader():
    client = Client([])
    client.server_settings["readonly"] = SimpleNamespace(value="0", readonly=0)
    with pytest.raises(ValueError, match="CANDLE_READ_SETTINGS_UNVERIFIED"):
        ClosedMarketReader(client, clock_ms=lambda: CLOSED)


@pytest.mark.parametrize("value", ["0", 30000, False])
def test_existing_client_setting_cannot_override_verified_profile(value):
    client = Client([])
    client.get_client_setting = lambda key: value if key == "max_result_rows" else None
    with pytest.raises(ValueError, match="CANDLE_READ_SETTINGS_UNVERIFIED"):
        ClosedMarketReader(client, clock_ms=lambda: CLOSED)


@pytest.mark.parametrize(
    "result", [None, [], {"unrecognized": 1}, {"max_threads": 2}, {"readonly": True}]
)
def test_validator_output_cannot_remove_or_mutate_budget_truth(result):
    client = Client([])
    client._validate_settings = lambda _: result
    with pytest.raises(ValueError, match="CANDLE_READ_SETTINGS_UNVERIFIED"):
        ClosedMarketReader(client, clock_ms=lambda: CLOSED)


def test_validator_exception_is_fixed_and_does_not_query():
    client = Client([])

    def fail(_):
        raise RuntimeError("secret=must-not-print")

    client._validate_settings = fail
    with pytest.raises(ValueError) as caught:
        ClosedMarketReader(client, clock_ms=lambda: CLOSED)
    assert str(caught.value) == "CANDLE_READ_SETTINGS_UNVERIFIED"
    assert client.calls == []


@pytest.mark.parametrize(
    "change", ["limit", "retries", "timeout", "profile", "client_setting"]
)
def test_capabilities_are_rechecked_before_every_query(change):
    subject, client = reader([row(CLOSED - MINUTE)])
    if change == "limit":
        client.query_limit = 1440
    elif change == "retries":
        client.query_retries = 2
    elif change == "timeout":
        client._send_receive_timeout = 300
    elif change == "profile":
        client.server_settings["max_result_bytes"].value = "0"
    else:
        client.get_client_setting = lambda key: (
            "1" if key == "max_result_rows" else None
        )
    with pytest.raises(ValueError, match="CANDLE_READ_"):
        subject.read("BTCUSDT", closed_at_ms=CLOSED, minutes=1)
    assert client.calls == []


def installed_client_without_network():
    # Deliberately bypass __init__: it auto-connects. Only installed validation
    # methods run; no live profile/server/transport is accessed in this test.
    from clickhouse_connect.driver.httpclient import HttpClient
    from clickhouse_connect.driver.models import SettingDef

    client = object.__new__(HttpClient)
    client.params = {}
    client.query_limit = 0
    client.query_retries = 0
    client._send_receive_timeout = 10
    client.server_settings = {
        key: SettingDef(key, value.value, value.readonly)
        for key, value in Client([]).server_settings.items()
    }

    def no_network(*args, **kwargs):
        pytest.fail("offline client capability test must not query a server")

    client.query = no_network
    return client


def test_installed_driver_accepts_same_fixed_profile_by_omitting_redundant_settings():
    client = installed_client_without_network()
    subject = ClosedMarketReader(client, clock_ms=lambda: CLOSED)
    assert client._validate_settings(subject._settings()) == {}
    assert subject._check_client() == subject._settings()


@pytest.mark.parametrize("invalid_action", ["error", "drop", "send"])
def test_installed_driver_existing_shadow_profile_cannot_drop_or_send_incompatible_limits(
    monkeypatch, caplog, invalid_action
):
    from clickhouse_connect import common
    from clickhouse_connect.driver.models import SettingDef

    client = installed_client_without_network()
    client.server_settings["max_execution_time"] = SettingDef(
        "max_execution_time", "3", 1
    )
    client.server_settings["max_result_rows"] = SettingDef(
        "max_result_rows", "20000", 1
    )
    client.server_settings["max_result_bytes"] = SettingDef("max_result_bytes", "0", 1)
    prior = common.get_setting
    monkeypatch.setattr(
        common,
        "get_setting",
        lambda key: invalid_action if key == "invalid_setting_action" else prior(key),
    )
    with pytest.raises(ValueError, match="CANDLE_READ_SETTINGS_UNVERIFIED"):
        ClosedMarketReader(client, clock_ms=lambda: CLOSED)
    assert not caplog.records  # Rejected before the driver's warning/drop path.


def test_installed_driver_limit_rewrite_exists_but_reader_refuses_client():
    from clickhouse_connect.driver.query import QueryContext

    client = installed_client_without_network()
    client.query_limit = 1440
    context = QueryContext(
        query="SELECT open_ms FROM v2_market_candles_incremental",
        parameters={},
        settings={},
    )
    assert client._prep_query(context).endswith("LIMIT 1440")
    with pytest.raises(ValueError, match="CANDLE_READ_CLIENT_LIMIT_FORBIDDEN"):
        ClosedMarketReader(client, clock_ms=lambda: CLOSED)
