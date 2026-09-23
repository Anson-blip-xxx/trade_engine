import copy
import json
from dataclasses import asdict
from uuid import uuid4

import pytest
from test_v2_account_coverage import Account
from test_v2_intent_admission import database as database_fixture
from test_v2_protection_child import SCOPE

from v2_core.state import BusinessState, StateKey
from v2_core.venue_settings import TestnetSymbolSettings

database = database_fixture


class SettingsVenue(Account):
    def __init__(self, *, leverage=20, margin_type="CROSSED"):
        super().__init__()
        self.config = {
            "symbol": "BTCUSDT",
            "leverage": leverage,
            "marginType": margin_type,
            "isAutoAddMargin": False,
        }
        self.posts = []
        self.post_outcome = "APPLY"

    def __call__(self, method, path, params):
        if path == "/fapi/v1/symbolConfig":
            assert method == "GET" and params == {"symbol": "BTCUSDT"}
            self.calls.append(path)
            return [copy.deepcopy(self.config)]
        if method == "POST" and path in {
            "/fapi/v1/marginType",
            "/fapi/v1/leverage",
        }:
            self.posts.append((path, copy.deepcopy(params)))
            if self.post_outcome != "IGNORE":
                if path.endswith("marginType"):
                    self.config["marginType"] = params["marginType"]
                else:
                    self.config["leverage"] = params["leverage"]
            if self.post_outcome == "AMBIGUOUS":
                raise TimeoutError("sensitive request")
            return {"ok": True}
        return super().__call__(method, path, params)


def order():
    return {
        "order_id": str(uuid4()),
        "symbol": "BTCUSDT",
        "leg": "OPEN",
        "reduce_only": False,
    }


def coordinator(database, venue, *, enabled):
    return TestnetSymbolSettings(
        database,
        venue,
        scope=SCOPE,
        clock_ms=lambda: 1000,
        allow_writes=enabled,
    )


def state(database, order_id):
    key = StateKey(
        **asdict(SCOPE), namespace="venue-symbol-settings-v1", key=order_id
    )
    snapshot = BusinessState(database).read(key)
    return None if snapshot is None else json.loads(snapshot.payload_json)


def test_matching_settings_need_no_write_or_state(database):
    venue, item = SettingsVenue(leverage=2, margin_type="ISOLATED"), order()
    result = coordinator(database, venue, enabled=True).ensure(
        item, {"leverage": 2, "margin_type": "ISOLATED"}
    )
    assert result["status"] == "VERIFIED"
    assert result["state_id"] is None
    assert result["changed"] == [] and venue.posts == []
    assert state(database, item["order_id"]) is None


def test_disabled_mismatch_is_read_only(database):
    venue, item = SettingsVenue(), order()
    result = coordinator(database, venue, enabled=False).ensure(
        item, {"leverage": 2, "margin_type": "ISOLATED"}
    )
    assert result["status"] == "WRITE_DISABLED"
    assert result["state_id"] is None
    assert venue.posts == [] and state(database, item["order_id"]) is None
    assert venue.calls == ["/fapi/v1/symbolConfig"]


def test_flat_account_is_registered_before_bounded_writes(database):
    venue, item = SettingsVenue(), order()

    original = venue.__class__.__call__

    def checked(self, method, path, params):
        if method == "POST":
            payload = state(database, item["order_id"])
            assert payload["status"] == "CONFIGURING"
            assert payload["execution_authorized"] is False
        return original(self, method, path, params)

    venue.__class__.__call__ = checked
    try:
        result = coordinator(database, venue, enabled=True).ensure(
            item, {"leverage": 2, "margin_type": "ISOLATED"}
        )
    finally:
        venue.__class__.__call__ = original
    assert result["status"] == "VERIFIED"
    assert result["changed"] == ["margin_type", "leverage"]
    assert [row[0] for row in venue.posts] == [
        "/fapi/v1/marginType",
        "/fapi/v1/leverage",
    ]
    assert state(database, item["order_id"])["status"] == "VERIFIED"


def test_ambiguous_writes_are_resolved_by_get_and_never_retried(database):
    venue, item = SettingsVenue(), order()
    venue.post_outcome = "AMBIGUOUS"
    result = coordinator(database, venue, enabled=True).ensure(
        item, {"leverage": 2, "margin_type": "ISOLATED"}
    )
    assert result["status"] == "VERIFIED"
    assert len(venue.posts) == 2


def test_unconfirmed_write_fails_closed(database):
    venue, item = SettingsVenue(), order()
    venue.post_outcome = "IGNORE"
    result = coordinator(database, venue, enabled=True).ensure(
        item, {"leverage": 2, "margin_type": "ISOLATED"}
    )
    assert result["status"] == "UNCONFIRMED"
    assert len(venue.posts) == 1
    assert state(database, item["order_id"])["status"] == "CONFIGURING"


@pytest.mark.parametrize("exposure", ["position", "ordinary", "conditional"])
def test_nonempty_account_blocks_before_any_write(database, exposure):
    venue, item = SettingsVenue(), order()
    if exposure == "position":
        venue.rows["/fapi/v3/positionRisk"] = [
            {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1"}
        ]
    elif exposure == "ordinary":
        venue.rows["/fapi/v1/openOrders"] = [{"symbol": "BTCUSDT", "orderId": 1}]
    else:
        venue.rows["/fapi/v1/openAlgoOrders"] = [
            {"symbol": "BTCUSDT", "algoId": 1}
        ]
    result = coordinator(database, venue, enabled=True).ensure(
        item, {"leverage": 2, "margin_type": "ISOLATED"}
    )
    assert result["status"] == "ACCOUNT_NOT_FLAT"
    assert venue.posts == [] and state(database, item["order_id"]) is None


def test_explicit_external_position_does_not_block_other_symbol_settings(database):
    venue, item = SettingsVenue(), order()
    venue.rows["/fapi/v3/positionRisk"] = [
        {"symbol": "ZORAUSDT", "positionSide": "BOTH", "positionAmt": "26399"}
    ]
    service = TestnetSymbolSettings(
        database,
        venue,
        scope=SCOPE,
        clock_ms=lambda: 1000,
        allow_writes=True,
        excluded_position_symbols=("ZORAUSDT",),
    )
    assert service.ensure(
        item, {"leverage": 2, "margin_type": "ISOLATED"}
    )["status"] == "VERIFIED"
    excluded = {**item, "order_id": str(uuid4()), "symbol": "ZORAUSDT"}
    with pytest.raises(ValueError, match="excluded"):
        service.ensure(excluded, {"leverage": 2, "margin_type": "ISOLATED"})


@pytest.mark.parametrize(
    "terms",
    [
        {"leverage": 0, "margin_type": "ISOLATED"},
        {"leverage": 6, "margin_type": "ISOLATED"},
        {"leverage": 2, "margin_type": "CROSS"},
        {"leverage": True, "margin_type": "ISOLATED"},
        {"leverage": 2, "margin_type": "ISOLATED", "extra": 1},
    ],
)
def test_terms_are_strictly_bounded_before_io(database, terms):
    venue = SettingsVenue()
    with pytest.raises(ValueError, match="bounded"):
        coordinator(database, venue, enabled=True).ensure(order(), terms)
    assert venue.calls == [] and venue.posts == []
