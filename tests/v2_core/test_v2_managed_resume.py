"""Resume readiness uses actual PG exposure and stop coverage, no trading I/O."""

from copy import deepcopy

import pytest
from test_v2_account_coverage import SCOPE, covered, database, setup

from services import v2_managed_testnet as managed
from v2_core.runtime_policy import PolicyStore

__all__ = ["covered", "database", "setup"]


@pytest.fixture
def ready(database, covered, monkeypatch, request):
    _, reader, _ = covered
    policy = PolicyStore(database, SCOPE)
    if getattr(request, "param", True):
        policy.patch(
            {"capital.pool_fraction": "0.70"}, expected_version=0, reason="QA_RESUME"
        )
    state = {"trade": True, "calls": []}

    class Request:
        account_id, environment = SCOPE.account_id, SCOPE.environment

        def __call__(self, method, path, params):
            assert method == "GET", "resume must not change exchange state"
            state["calls"].append(path)
            if path == "/fapi/v1/accountConfig":
                return {"canTrade": state["trade"]}
            return reader(method, path, params)

    monkeypatch.setattr(managed, "database", lambda: database)
    monkeypatch.setattr(managed, "scope_for", lambda *_: SCOPE)
    monkeypatch.setattr(managed, "readonly_request", lambda *_: Request())
    return managed.TestnetPorts("qa-tenant", {}), reader, policy, state


def test_resume_accepts_owned_protected_exposure_without_resetting_policy(ready):
    ports, reader, policy, state = ready
    before = policy.read()
    rows = deepcopy(reader.rows)
    assert ports.resume_preflight({}) == []
    assert policy.read() == before and reader.rows == rows
    assert "/fapi/v1/openAlgoOrders" in state["calls"]


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("stop", "MISSING_CONFIRMED_STOP"),
        ("quantity", "VENUE_LEDGER_POSITION_MISMATCH"),
        ("unknown_order", "UNOWNED_ORDINARY_ORDER"),
        ("permission", "TRADE_PERMISSION_UNVERIFIED"),
        ("network", "INCOMPLETE_ACCOUNT_READ"),
        ("stop_terms", "PROTECTION_TERMS_MISMATCH"),
    ],
)
def test_resume_readiness_fails_closed(ready, failure, expected):
    ports, reader, _, state = ready
    if failure == "stop":
        reader.rows["/fapi/v1/openAlgoOrders"] = []
    elif failure == "quantity":
        reader.rows["/fapi/v3/positionRisk"][0]["positionAmt"] = "2"
    elif failure == "unknown_order":
        reader.rows["/fapi/v1/openOrders"] = [{"symbol": "BTCUSDT", "orderId": 999}]
    elif failure == "permission":
        state["trade"] = False
    elif failure == "network":
        reader.fail = "/fapi/v3/account"
    else:
        reader.rows["/fapi/v1/openAlgoOrders"][0]["triggerPrice"] = "1"
    assert expected in ports.resume_preflight({})


@pytest.mark.parametrize("ready", [False], indirect=True)
def test_resume_requires_existing_policy_and_does_not_initialize_one(ready):
    ports, _, policy, state = ready
    assert ports.resume_preflight({}) == ["RESUME_EXISTING_POLICY_REQUIRED"]
    assert policy.read().version == 0
    assert not state["calls"]


def test_resume_binding_failure_is_sanitized(ready, monkeypatch):
    ports, _, _, state = ready

    def changed(*_):
        raise ValueError("private-credential-details")

    monkeypatch.setattr(managed, "scope_for", changed)
    assert ports.resume_preflight({}) == ["RESUME_READINESS_UNAVAILABLE"]
    assert not state["calls"]
