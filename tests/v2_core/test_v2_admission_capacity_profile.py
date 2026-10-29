"""Isolated PG acceptance for a proposed 2000/550 host-budget profile.

This is not approval of a venue rate limit: production activation still needs
its actual exchange limit checked. HTTP error responses below are injected into
the real transport adapter; no external request or runtime policy is changed.
"""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_shared_market import shared as shared_fixture
from test_v2_transport import Connection

from services.v2_testnet_daemon_entry import PrivateRatePermit
from v2_core.shared_market import AdmissionBudget, HostBudget, MarketPolicy
from v2_core.state import BusinessState, StateKey
from v2_core.transport import BinanceSignedTransport, ExchangeTransportError

database = database_fixture
shared = shared_fixture
HOST = "demo-fapi.binance.com"
KEY = StateKey(
    "BINANCE", "market-data", "SANDBOX", "FUTURES", "shared-market-policy", "current"
)


def profile(connect, *, cap=550, expected_version=0):
    policy = MarketPolicy(shared_weight_limit=2000, market_weight_limit=cap)
    result = BusinessState(connect).change(
        KEY,
        expected_version=expected_version,
        request_key=f"qa-capacity-profile-{expected_version + 1}",
        reason="ISOLATED_QA_ONLY_NOT_VENUE_LIMIT_APPROVAL",
        payload=policy.values(),
    )
    assert result.code == "APPLIED"
    return policy


def buckets(connect):
    return (
        HostBudget(connect, environment="SANDBOX", lane="market"),
        AdmissionBudget(connect, environment="SANDBOX"),
        HostBudget(connect, environment="SANDBOX", lane="management"),
    )


def spend(bucket, weight):
    while weight:
        request_weight = min(100, weight)
        assert bucket.permit(request_weight)
        weight -= request_weight


def ledger(connect):
    with connect() as conn:
        return conn.execute(
            "SELECT window_id,used_weight,market_weight,blocked_until::text "
            "FROM v2_egress_budgets WHERE host=%s",
            (HOST,),
        ).fetchone()


def noncritical_denied(market, admission):
    assert market.permit(1) is False
    with pytest.raises(ValueError, match="^ADMISSION_BUDGET_UNAVAILABLE$"):
        admission.permit(1)


def test_550_is_one_combined_cap_leaving_1450_for_management(shared):
    policy = profile(shared)
    assert policy.shared_weight_limit - policy.market_weight_limit == 1450
    market, admission, management = buckets(shared)
    spend(market, 300)
    spend(admission, 250)
    before = ledger(shared)
    assert before[1:3] == (550, 550)
    noncritical_denied(market, admission)
    assert ledger(shared) == before
    spend(management, 1450)
    after = ledger(shared)
    assert after[0] == before[0]
    assert after[1:3] == (2000, 550)
    assert management.permit(1) is False
    noncritical_denied(market, admission)
    assert ledger(shared) == after


def test_management_can_use_idle_capacity_but_every_lane_shares_2000_total(shared):
    profile(shared)
    market, admission, management = buckets(shared)
    spend(management, 2000)
    before = ledger(shared)
    assert before[1:3] == (2000, 0)
    assert management.permit(1) is False
    noncritical_denied(market, admission)
    assert ledger(shared) == before


def test_concurrent_market_and_admission_instances_share_atomic_550_counter(shared):
    profile(shared)

    def reserve(index):
        budget = HostBudget(
            shared,
            environment="SANDBOX",
            lane="market" if index % 2 else "admission",
        )
        return budget.permit(50)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(24)))
    assert sum(results) == 11
    assert ledger(shared)[1:3] == (550, 550)
    market, admission, management = buckets(shared)
    noncritical_denied(market, admission)
    spend(management, 1450)
    assert ledger(shared)[1:3] == (2000, 550)
    assert management.permit(1) is False


def test_concurrent_all_lanes_cannot_exceed_total_or_noncritical_cap(shared):
    profile(shared)
    market, _, management = buckets(shared)
    spend(market, 450)
    spend(management, 1400)
    before = ledger(shared)
    assert before[1:3] == (1850, 450)

    def reserve(index):
        lane = ("market", "admission", "management")[index % 3]
        allowed = HostBudget(shared, environment="SANDBOX", lane=lane).permit(50)
        return lane, allowed

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(24)))
    assert sum(allowed for _, allowed in results) == 3
    noncritical = sum(allowed for lane, allowed in results if lane != "management")
    assert noncritical <= 2
    after = ledger(shared)
    assert after[0] == before[0]
    assert after[1:3] == (2000, 450 + 50 * noncritical)
    assert management.permit(1) is False


@pytest.mark.parametrize("http_status", [429, 418])
def test_actual_transport_penalty_blocks_all_three_lanes_despite_reserve(
    shared, http_status
):
    profile(shared)
    market, admission, management = buckets(shared)
    response = Connection()
    response.status = http_status
    response.raw = json.dumps(
        {"code": -1003, "msg": "private exchange response"}
    ).encode()
    request = BinanceSignedTransport(
        account_id="capacity-profile-qa",
        environment="SANDBOX",
        api_key="dummy-key",
        api_secret="dummy-secret",
        clock_ms=lambda: 1000,
        permit=PrivateRatePermit(
            admission, entries=False, protection=False, exits=False
        ),
        connection_factory=lambda *_args, **_kwargs: response,
    )
    with pytest.raises(ExchangeTransportError) as caught:
        request("GET", "/fapi/v3/account", {})
    assert caught.value.status == http_status and caught.value.code == -1003
    assert str(caught.value) == "EXCHANGE_RESPONSE_ERROR"
    assert len(response.calls) == 1 and response.closed
    before = ledger(shared)
    assert before[1:3] == (5, 5)
    with shared() as conn:
        assert conn.execute(
            "SELECT blocked_until>clock_timestamp() FROM v2_egress_budgets WHERE host=%s",
            (HOST,),
        ).fetchone() == (True,)
    noncritical_denied(market, admission)
    assert management.permit(1) is False
    assert ledger(shared) == before


def test_raise_350_to_550_preserves_consumed_weight_in_same_window(shared):
    profile(shared, cap=350)
    market, admission, management = buckets(shared)
    spend(market, 200)
    spend(admission, 150)
    spend(management, 300)
    noncritical_denied(market, admission)
    before = ledger(shared)
    assert before[1:3] == (650, 350)
    profile(shared, cap=550, expected_version=1)
    assert ledger(shared) == before
    assert MarketPolicy.read(shared, "SANDBOX").market_weight_limit == 550
    spend(admission, 100)
    spend(market, 100)
    after = ledger(shared)
    assert after[0] == before[0]
    assert after[1:3] == (850, 550)
    noncritical_denied(market, admission)
    assert ledger(shared) == after
    spend(management, 1150)
    assert ledger(shared)[1:3] == (2000, 550)
    assert management.permit(1) is False
