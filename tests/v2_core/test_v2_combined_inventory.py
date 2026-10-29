"""Optional accountConfig acquisition preserves evidence and fails closed."""

import copy
import json
import threading
import time
from uuid import uuid4

import pytest

from v2_core.account_inventory import (
    ACCOUNT_CONFIG_PATH,
    COMBINED_ROUTES,
    ROUTES,
    AccountInventory,
    account_config_derivations,
)
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.transport import ExchangeTransportError

SCOPE = AccountScope("BINANCE", "combined-inventory-qa", "SANDBOX", "FUTURES")
CONFIG = {
    "feeTier": 0,
    "canTrade": True,
    "canDeposit": True,
    "canWithdraw": True,
    "dualSidePosition": False,
    "updateTime": 0,
    "multiAssetsMargin": False,
    "tradeGroupId": -1,
}


class Reader:
    account_id = SCOPE.account_id
    environment = SCOPE.environment

    def __init__(self):
        self.calls = []
        self.rows = {
            ACCOUNT_CONFIG_PATH: copy.deepcopy(CONFIG),
            "/fapi/v1/positionSide/dual": {"dualSidePosition": False},
            "/fapi/v1/multiAssetsMargin": {"multiAssetsMargin": False},
            "/fapi/v3/positionRisk": [],
            "/fapi/v3/account": {
                "totalWalletBalance": "5000",
                "availableBalance": "5000",
                "totalMarginBalance": "5000",
            },
            "/fapi/v1/openOrders": [],
            "/fapi/v1/openAlgoOrders": [],
        }
        self.failure = None

    def __call__(self, method, path, params):
        assert method == "GET" and params == {}
        self.calls.append(path)
        if path == ACCOUNT_CONFIG_PATH and self.failure:
            raise self.failure
        return copy.deepcopy(self.rows[path])


def collect(reader=None, *, workers=1, **kwargs):
    return AccountInventory(
        reader or Reader(),
        scope=SCOPE,
        clock_ms=kwargs.pop("clock_ms", lambda: 1000),
        read_workers=workers,
        combined_account_config=kwargs.pop("combined_account_config", True),
        **kwargs,
    ).collect(str(uuid4()))


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_six_actual_gets_and_source_bound_derivations(workers):
    reader = Reader()
    result = collect(reader, workers=workers)
    assert len(reader.calls) == 6
    assert reader.calls[0] == reader.calls[-1] == "/fapi/v3/positionRisk"
    assert set(result["responses"]) == {name for name, _ in COMBINED_ROUTES}
    assert result["responses"]["account_config"] == CONFIG
    assert result["response_digests"] == {
        name: digest(canonical({"response": response}))
        for name, response in result["responses"].items()
    }
    assert result["derived_responses"] == account_config_derivations(CONFIG)
    for name, field in (
        ("mode", "dualSidePosition"),
        ("margin_mode", "multiAssetsMargin"),
    ):
        assert result["derived_responses"][name] == {
            "response": {field: False},
            "source_response": "account_config",
            "source_path": ACCOUNT_CONFIG_PATH,
            "source_digest": result["response_digests"]["account_config"],
        }
    assert "mode" not in result["responses"]
    assert "margin_mode" not in result["responses"]
    assert result["status"] == "CLEAR_FOR_RECOVERY_CHECKS"
    assert result["execution_authorized"] is False
    assert result["summary"]["trade_permission_verified"] is False


def test_default_keeps_seven_original_reads_and_evidence_shape():
    reader = Reader()
    result = AccountInventory(reader, scope=SCOPE, clock_ms=lambda: 1000).collect(
        str(uuid4())
    )
    assert reader.calls == [path for _, path in ROUTES]
    assert len(result["response_digests"]) == 7
    assert "derived_responses" not in result
    assert "account_config" not in result["responses"]


def test_policy_callback_is_rechecked_once_per_collection_without_caching():
    reader = Reader()
    switches = iter([True, False, True])
    calls = []

    def enabled():
        calls.append(True)
        return next(switches)

    inventory = AccountInventory(
        reader, scope=SCOPE, clock_ms=lambda: 1000, combined_account_config=enabled
    )
    for count in (6, 7, 6):
        reader.calls.clear()
        result = inventory.collect(str(uuid4()))
        assert len(reader.calls) == count
        assert not result["execution_authorized"]
    assert len(calls) == 3


@pytest.mark.parametrize(
    "value", [None, 0, 1, "true", "false", [], {}, lambda: None, lambda: 1]
)
def test_non_bool_switch_rejected_before_http(value):
    reader = Reader()
    with pytest.raises(ValueError, match="combined account"):
        collect(reader, combined_account_config=value)
    assert not reader.calls


@pytest.mark.parametrize("field", ["dualSidePosition", "multiAssetsMargin"])
@pytest.mark.parametrize("value", [None, "false", "true", 0, 1, [], {}])
def test_non_boolean_modes_block_with_raw_evidence_retained(field, value):
    reader = Reader()
    reader.rows[ACCOUNT_CONFIG_PATH][field] = value
    result = collect(reader)
    assert "INVALID_ACCOUNT_RESPONSE" in result["blockers"]
    assert result["status"] == "BLOCKED"
    assert result["derived_responses"] == {}
    assert result["responses"]["account_config"][field] == value
    assert not result["execution_authorized"]
    assert len(reader.calls) == 6


@pytest.mark.parametrize(
    "field,blocker",
    [
        ("dualSidePosition", "ONE_WAY_MODE_REQUIRED"),
        ("multiAssetsMargin", "SINGLE_ASSET_MODE_REQUIRED"),
    ],
)
def test_true_mode_is_a_real_observation_but_blocks(field, blocker):
    reader = Reader()
    reader.rows[ACCOUNT_CONFIG_PATH][field] = True
    result = collect(reader)
    assert blocker in result["blockers"]
    assert result["status"] == "BLOCKED"
    assert result["derived_responses"]
    assert not result["execution_authorized"]


@pytest.mark.parametrize("field", ["dualSidePosition", "multiAssetsMargin"])
def test_missing_mode_is_not_assumed_false(field):
    reader = Reader()
    reader.rows[ACCOUNT_CONFIG_PATH].pop(field)
    assert "INVALID_ACCOUNT_RESPONSE" in collect(reader)["blockers"]


@pytest.mark.parametrize("response", [None, [], "false", False, 0])
def test_non_object_config_is_rejected(response):
    reader = Reader()
    reader.rows[ACCOUNT_CONFIG_PATH] = response
    result = collect(reader)
    assert "INVALID_ACCOUNT_RESPONSE" in result["blockers"]
    assert result["derived_responses"] == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("futureField", {"new": [1, True]}),
        ("canTrade", "true"),
        ("feeTier", None),
        ("updateTime", "not-a-freshness-proof"),
    ],
)
def test_uninterpreted_venue_fields_are_retained_without_new_permissions(field, value):
    reader = Reader()
    reader.rows[ACCOUNT_CONFIG_PATH][field] = value
    result = collect(reader)
    assert result["status"] == "CLEAR_FOR_RECOVERY_CHECKS"
    assert result["responses"]["account_config"][field] == value
    assert result["derived_responses"]["mode"]["source_digest"] == digest(
        canonical({"response": reader.rows[ACCOUNT_CONFIG_PATH]})
    )
    assert not result["execution_authorized"]
    assert not result["summary"]["trade_permission_verified"]


def test_optional_config_metadata_is_not_required_or_used_as_permission():
    reader = Reader()
    reader.rows[ACCOUNT_CONFIG_PATH] = {
        "dualSidePosition": False,
        "multiAssetsMargin": False,
    }
    result = collect(reader)
    assert result["status"] == "CLEAR_FOR_RECOVERY_CHECKS"
    assert not result["summary"]["trade_permission_verified"]


@pytest.mark.parametrize("workers", [1, 4])
def test_config_transport_failure_never_falls_back_and_preserves_safe_codes(workers):
    reader = Reader()
    reader.failure = ExchangeTransportError(
        "EXCHANGE_RESPONSE_ERROR", status=400, code=-2015
    )
    result = collect(reader, workers=workers)
    assert result["status"] == "BLOCKED"
    assert result["failures"] == {"account_config": "ACCOUNT_READ_UNAVAILABLE"}
    assert result["failure_details"] == {
        "account_config": {
            "category": "EXCHANGE_RESPONSE_ERROR",
            "http_status": 400,
            "exchange_code": -2015,
        }
    }
    assert "INCOMPLETE_ACCOUNT_READ" in result["blockers"]
    assert not result["execution_authorized"]
    assert result["derived_responses"] == {}
    assert "/fapi/v1/positionSide/dual" not in reader.calls
    assert "/fapi/v1/multiAssetsMargin" not in reader.calls
    assert reader.calls.count("/fapi/v3/positionRisk") == 1


def test_config_failure_does_not_expose_exception_contents():
    reader = Reader()
    reader.failure = RuntimeError("sensitive signed URL and API key")
    assert "sensitive" not in json.dumps(collect(reader))


@pytest.mark.parametrize("workers", [1, 4])
def test_deadline_stops_http_and_reserved_update_time_cannot_bypass_it(workers):
    reader = Reader()
    reader.rows[ACCOUNT_CONFIG_PATH]["updateTime"] = 4133980800000
    times = iter([1000, 40000, 40000])
    result = collect(reader, workers=workers, clock_ms=lambda: next(times))
    assert not reader.calls
    assert "ACQUISITION_DEADLINE" in result["blockers"]
    assert not result["execution_authorized"]


@pytest.mark.parametrize("workers", [1, 4])
def test_position_changes_still_block(workers):
    class Moving(Reader):
        def __call__(self, method, path, params):
            response = super().__call__(method, path, params)
            if path == "/fapi/v3/positionRisk" and self.calls.count(path) == 2:
                return [
                    {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1"}
                ]
            return response

    result = collect(Moving(), workers=workers)
    assert "POSITION_CHANGED_DURING_INVENTORY" in result["blockers"]
    assert "EXISTING_POSITION_REQUIRES_RECOVERY" in result["blockers"]
    assert not result["execution_authorized"]


def test_parallel_config_reads_are_inside_both_position_boundaries():
    class Observed(Reader):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()
            self.active = self.peak = self.position_reads = 0

        def __call__(self, method, path, params):
            with self.lock:
                if path == "/fapi/v3/positionRisk":
                    assert self.active == 0
                    self.position_reads += 1
                    return super().__call__(method, path, params)
                assert self.position_reads == 1
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                time.sleep(0.02)
                return super().__call__(method, path, params)
            finally:
                with self.lock:
                    self.active -= 1

    reader = Observed()
    result = collect(reader, workers=4)
    assert result["status"] == "CLEAR_FOR_RECOVERY_CHECKS"
    assert reader.active == 0 and reader.position_reads == 2 and reader.peak == 4


def test_raw_non_mode_fields_change_raw_and_derived_source_digests():
    reader = Reader()
    first = collect(reader)
    reader.rows[ACCOUNT_CONFIG_PATH]["feeTier"] = 1
    second = collect(reader)
    assert (
        first["response_digests"]["account_config"]
        != second["response_digests"]["account_config"]
    )
    assert (
        first["derived_responses"]["mode"]["response"]
        == second["derived_responses"]["mode"]["response"]
    )
    assert (
        first["derived_responses"]["mode"]["source_digest"]
        != second["derived_responses"]["mode"]["source_digest"]
    )
