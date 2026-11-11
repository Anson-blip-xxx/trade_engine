"""Atomic read groups and fair host quotas; isolated PG, never venue network."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from test_v2_shared_market import shared as shared_fixture

from tests.support.v2_admission import database as database_fixture
from v2_core.shared_market import (
    AdmissionBudget,
    AdmissionBudgetPolicy,
    HostBudget,
    MarketPolicy,
)
from v2_core.state import BusinessState, StateKey

database = database_fixture
shared = shared_fixture
HOST = "demo-fapi.binance.com"
ACCOUNT = "group-budget-qa"
TARGET = "BTCUSDT"
INVALID = "^ADMISSION_CONTEXT_TICKET_INVALID$"
UNAVAILABLE = "^ADMISSION_BUDGET_UNAVAILABLE$"


def configure(connect, *, reserved=0, market_reserved=0, expected_version=0):
    result = BusinessState(connect).change(
        StateKey(
            "BINANCE",
            "market-data",
            "SANDBOX",
            "FUTURES",
            "admission-budget-policy",
            "current",
        ),
        expected_version=expected_version,
        request_key=f"qa-admission-share-{expected_version}",
        reason="ISOLATED_QA",
        payload=AdmissionBudgetPolicy(reserved, market_reserved).values(),
    )
    assert result.code == "APPLIED"


@pytest.fixture
def budget(shared, monkeypatch):
    # Deterministically model the clock that the locked PG query returns. No
    # test waits for a wall-clock minute or weakens production clock checks.
    clock = [1_800_000_001_000]
    original = HostBudget._locked_state

    def locked(self, conn):
        window, used, market, blocked, _ = original(self, conn)
        return window, used, market, blocked, clock[0]

    monkeypatch.setattr(HostBudget, "_locked_state", locked)
    result = BusinessState(shared).change(
        StateKey(
            "BINANCE",
            "market-data",
            "SANDBOX",
            "FUTURES",
            "shared-market-policy",
            "current",
        ),
        expected_version=0,
        request_key="qa-context-group-host",
        reason="ISOLATED_QA",
        payload=MarketPolicy(
            shared_weight_limit=2000, market_weight_limit=550
        ).values(),
    )
    assert result.code == "APPLIED"
    return shared, clock, AdmissionBudget(shared, environment="SANDBOX")


def reserve(bucket, clock, *, adaptive=False, **changes):
    values = {
        "account_id": ACCOUNT,
        "symbol": TARGET,
        "adaptive": adaptive,
        "deadline_ms": clock[0] + 15_000,
    }
    return bucket.reserve_context(**{**values, **changes})


def take(bucket, ticket, *, path="/fapi/v3/account", params=None, weight=5, **changes):
    values = {
        "account_id": ACCOUNT,
        "environment": "SANDBOX",
        "method": "GET",
        "path": path,
        "params": {} if params is None else params,
        "weight": weight,
    }
    return bucket.consume_context(ticket, **{**values, **changes})


def ledger(connect):
    with connect() as conn:
        return conn.execute(
            "SELECT window_id,used_weight,market_weight FROM v2_egress_budgets WHERE host=%s",
            (HOST,),
        ).fetchone()


def spend(bucket, weight):
    while weight:
        chunk = min(weight, 100)
        assert bucket.permit(chunk)
        weight -= chunk


def consume_all(bucket, ticket, adaptive):
    for path, params, weight, count in (
        ("/fapi/v3/positionRisk", {}, 5, 2),
        ("/fapi/v3/account", {}, 5, 1),
        ("/fapi/v1/accountConfig", {}, 5, 1),
        ("/fapi/v1/symbolConfig", {"symbol": TARGET}, 5, 1),
        ("/fapi/v1/exchangeInfo", {}, 1, 1),
        ("/fapi/v1/premiumIndex", {"symbol": TARGET}, 1, 1),
    ) + (
        (
            ("/fapi/v1/leverageBracket", {"symbol": TARGET}, 1, 1),
            ("/fapi/v1/ticker/bookTicker", {"symbol": TARGET}, 2, 1),
        )
        if adaptive
        else ()
    ):
        for _ in range(count):
            assert take(bucket, ticket, path=path, params=params, weight=weight)


@pytest.mark.parametrize("adaptive,cost", [(False, 27), (True, 30)])
def test_exact_full_group_is_charged_once_and_preserves_management(
    budget, adaptive, cost
):
    connect, clock, bucket = budget
    market = HostBudget(connect, environment="SANDBOX", lane="market")
    spend(market, 550 - cost)
    ticket = reserve(bucket, clock, adaptive=adaptive)
    before = ledger(connect)
    assert before[1:] == (550, 550)
    consume_all(bucket, ticket, adaptive)
    assert ledger(connect) == before
    with pytest.raises(ValueError, match=INVALID):
        take(bucket, ticket)
    spend(HostBudget(connect, environment="SANDBOX"), 1450)
    assert ledger(connect)[1:] == (2000, 550)


@pytest.mark.parametrize("adaptive", [False, True])
def test_insufficient_group_never_partially_debits(budget, adaptive):
    connect, clock, bucket = budget
    spend(bucket, 525)
    before = ledger(connect)
    with pytest.raises(ValueError, match=UNAVAILABLE):
        reserve(bucket, clock, adaptive=adaptive)
    assert ledger(connect) == before


def test_loss_or_abandonment_never_refunds_or_implicitly_renews(budget):
    connect, clock, bucket = budget
    ticket = reserve(bucket, clock)
    assert take(bucket, ticket)
    del ticket
    assert ledger(connect)[1:] == (27, 27)
    reserve(bucket, clock)
    assert ledger(connect)[1:] == (54, 54)


@pytest.mark.parametrize(
    "changes",
    [
        {"account_id": "different-account"},
        {"environment": "LIVE"},
        {"method": "POST"},
        {"path": "/fapi/v1/order"},
        {"params": {"symbol": TARGET}},
        {"params": {"timestamp": "1"}},
        {"params": {"extra": True}},
        {"weight": 1},
        {"weight": True},
    ],
)
def test_wrong_binding_route_parameters_or_weight_never_consumes(budget, changes):
    connect, clock, bucket = budget
    ticket = reserve(bucket, clock)
    before = ledger(connect)
    with pytest.raises(ValueError, match=INVALID):
        take(bucket, ticket, **changes)
    assert take(bucket, ticket)
    assert ledger(connect) == before


def test_wrong_symbol_owner_or_host_never_borrows_a_ticket(budget):
    connect, clock, bucket = budget
    ticket = reserve(bucket, clock)
    with pytest.raises(ValueError, match=INVALID):
        take(bucket, ticket, path="/fapi/v1/symbolConfig", params={"symbol": "ETHUSDT"})
    for other in (
        AdmissionBudget(connect, environment="SANDBOX"),
        AdmissionBudget(connect, environment="LIVE"),
    ):
        with pytest.raises(ValueError, match=INVALID):
            take(other, ticket)
    with pytest.raises(ValueError, match=INVALID):
        take(bucket, object())
    assert ledger(connect)[1:] == (27, 27)


def test_concurrent_same_ticket_cannot_exceed_exact_endpoint_count(budget):
    connect, clock, bucket = budget
    ticket = reserve(bucket, clock)

    def consume(_):
        try:
            take(bucket, ticket, path="/fapi/v3/positionRisk")
        except ValueError as exc:
            assert str(exc) == "ADMISSION_CONTEXT_TICKET_INVALID"
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(consume, range(16))) == 2
    assert ledger(connect)[1:] == (27, 27)


@pytest.mark.parametrize(
    "fault", ["deadline", "minute", "backward", "host_future", "fork"]
)
def test_ticket_lifecycle_fails_closed_without_new_charge(budget, monkeypatch, fault):
    connect, clock, bucket = budget
    ticket = reserve(bucket, clock)
    before = ledger(connect)
    if fault == "deadline":
        clock[0] += 15_000
    elif fault == "minute":
        clock[0] += 60_000
    elif fault == "backward":
        clock[0] -= 1
    elif fault == "host_future":
        with connect() as conn:
            conn.execute("UPDATE v2_egress_budgets SET window_id=window_id+1")
        before = ledger(connect)
    else:
        monkeypatch.setattr("v2_core.shared_market.os.getpid", lambda: ticket.pid + 1)
    with pytest.raises(ValueError, match=INVALID if fault == "fork" else UNAVAILABLE):
        take(bucket, ticket)
    assert ledger(connect) == before


@pytest.mark.parametrize("offset", [0, -1, 60_001])
def test_invalid_original_deadline_does_not_debit(budget, offset):
    connect, clock, bucket = budget
    with pytest.raises(ValueError, match=UNAVAILABLE):
        reserve(bucket, clock, deadline_ms=clock[0] + offset)
    assert ledger(connect)[1:] == (0, 0)


def test_context_deadline_is_only_shortened_to_exact_minute_margin(budget):
    connect, clock, bucket = budget
    end = (clock[0] // 60000 + 1) * 60000
    exact = reserve(bucket, clock, deadline_ms=end - 500)
    clipped = reserve(bucket, clock, deadline_ms=end - 499)
    assert exact.deadline_ms == clipped.deadline_ms == end - 500
    assert ledger(connect)[1:] == (54, 54)


@pytest.mark.parametrize("second_ms", [44_499, 45_216, 45_968, 55_000, 58_000])
@pytest.mark.parametrize("adaptive", [False, True])
def test_late_minute_context_uses_remaining_window_without_more_weight(
    budget, second_ms, adaptive
):
    connect, clock, bucket = budget
    start = clock[0] // 60000 * 60000
    clock[0] = start + second_ms
    ticket = reserve(bucket, clock, adaptive=adaptive)
    assert ticket.deadline_ms == min(clock[0] + 15000, start + 59500)
    assert ticket.created_ms == clock[0]
    consume_all(bucket, ticket, adaptive)
    cost = 30 if adaptive else 27
    assert ledger(connect)[1:] == (cost, cost)
    assert not any(ticket.remaining.values())


def test_jev_fast_result_can_buy_fresh_context_after_second_45(budget):
    connect, clock, bucket = budget
    start = clock[0] // 60000 * 60000
    clock[0] = start + 44_000
    initial = reserve(bucket, clock, adaptive=True)
    consume_all(bucket, initial, True)
    # Actual incident: a fast model wakes the scheduler around second 46.
    # It must buy a NEW read group, not reuse initial balances or its ticket.
    clock[0] = start + 45_968
    fresh = reserve(bucket, clock, adaptive=True)
    assert fresh is not initial and fresh.deadline_ms == start + 59_500
    consume_all(bucket, fresh, True)
    assert ledger(connect)[1:] == (60, 60)
    with pytest.raises(ValueError, match=INVALID):
        take(bucket, initial)


@pytest.mark.parametrize("second_ms", [59_500, 59_501, 59_999])
def test_no_new_context_ticket_inside_final_safety_margin(budget, second_ms):
    connect, clock, bucket = budget
    clock[0] = clock[0] // 60000 * 60000 + second_ms
    with pytest.raises(ValueError, match=UNAVAILABLE):
        reserve(bucket, clock)
    assert ledger(connect)[1:] == (0, 0)


@pytest.mark.parametrize("advance_ms", [59_500, 60_000])
def test_shortened_ticket_cannot_spend_at_deadline_or_across_minutes(
    budget, advance_ms
):
    connect, clock, bucket = budget
    start = clock[0] // 60000 * 60000
    clock[0] = start + 46_000
    ticket = reserve(bucket, clock)
    before = ledger(connect)
    clock[0] = start + advance_ms
    with pytest.raises(ValueError, match=UNAVAILABLE):
        take(bucket, ticket)
    assert ledger(connect) == before


def test_ordinary_host_reservation_does_not_silently_clip_deadlines(budget):
    connect, clock, bucket = budget
    start = clock[0] // 60000 * 60000
    clock[0] = start + 46_000
    assert bucket._reserve(27, deadline_ms=clock[0] + 15000) is None
    assert ledger(connect)[1:] == (0, 0)


def test_host_penalty_blocks_reserved_gets_and_preserves_original_charge(budget):
    connect, clock, bucket = budget
    ticket = reserve(bucket, clock)
    before = ledger(connect)
    HostBudget(connect, environment="SANDBOX").penalize(60)
    with pytest.raises(ValueError, match=UNAVAILABLE):
        take(bucket, ticket)
    with pytest.raises(ValueError, match=UNAVAILABLE):
        reserve(bucket, clock)
    assert ledger(connect) == before


def test_database_errors_never_become_quota_success_or_local_denial(
    budget, monkeypatch
):
    connect, clock, bucket = budget
    ticket = reserve(bucket, clock)
    before = ledger(connect)

    def broken(*_args, **_kwargs):
        raise ConnectionError("QA_DATABASE_UNAVAILABLE")

    monkeypatch.setattr(AdmissionBudgetPolicy, "read", broken)
    with pytest.raises(ConnectionError, match="QA_DATABASE_UNAVAILABLE"):
        take(bucket, ticket)
    with pytest.raises(ConnectionError, match="QA_DATABASE_UNAVAILABLE"):
        reserve(bucket, clock)
    assert ledger(connect) == before


@pytest.mark.parametrize("first", ["market", "admission"])
def test_market_share_is_independent_not_double_reduced_by_admission(budget, first):
    connect, clock, bucket = budget
    configure(connect, reserved=120)
    market = HostBudget(connect, environment="SANDBOX", lane="market")
    if first == "market":
        spend(market, 430)
    for _ in range(4):
        reserve(bucket, clock, adaptive=True)
    if first == "admission":
        spend(market, 430)
    assert ledger(connect)[1:] == (550, 550)
    assert not market.permit(1)
    with pytest.raises(ValueError, match=UNAVAILABLE):
        reserve(bucket, clock)
    spend(HostBudget(connect, environment="SANDBOX"), 1450)
    assert ledger(connect)[1:] == (2000, 550)


def test_market_cannot_take_admission_share_even_when_total_is_idle(budget):
    connect, clock, bucket = budget
    configure(connect, reserved=120)
    market = HostBudget(connect, environment="SANDBOX", lane="market")
    spend(market, 430)
    before = ledger(connect)
    assert not market.permit(1)
    assert ledger(connect) == before
    for _ in range(4):
        reserve(bucket, clock, adaptive=True)
    assert ledger(connect)[1:] == (550, 550)


def test_default_zero_keeps_old_combined_cap_and_tracks_hot_enable(budget):
    connect, _, bucket = budget
    assert AdmissionBudgetPolicy.read(connect, "SANDBOX").values() == {
        "admission_reserved_weight": 0,
        "market_reserved_weight": 0,
    }
    market = HostBudget(connect, environment="SANDBOX", lane="market")
    spend(market, 400)
    spend(bucket, 100)
    configure(connect, reserved=120)
    spend(market, 30)
    before = ledger(connect)
    assert not market.permit(1)
    assert ledger(connect) == before
    assert bucket.permit(20)
    assert ledger(connect)[1:] == (550, 550)


def test_old_untracked_noncritical_weight_is_conservatively_counted(budget):
    connect, clock, _ = budget
    configure(connect, reserved=120)
    with connect() as conn:
        conn.execute(
            "INSERT INTO v2_egress_budgets(host,window_id,used_weight,market_weight) "
            "VALUES(%s,%s,500,500)",
            (HOST, clock[0] // 60000),
        )
    market = HostBudget(connect, environment="SANDBOX", lane="market")
    before = ledger(connect)
    assert not market.permit(1)
    assert ledger(connect) == before
    with connect() as conn:
        assert conn.execute(
            "SELECT used_weight FROM v2_public_market_budgets WHERE scope=%s",
            (market.market_lane_scope,),
        ).fetchone() == (500,)
    clock[0] += 60_000
    spend(market, 430)
    assert ledger(connect)[1:] == (430, 430)
    assert not market.permit(1)


def test_concurrent_market_and_groups_preserve_both_caps(budget):
    connect, clock, bucket = budget
    configure(connect, reserved=120)

    def attempt(index):
        if index % 2:
            allowed = HostBudget(connect, environment="SANDBOX", lane="market").permit(
                30
            )
            return "market", allowed
        try:
            reserve(bucket, clock, adaptive=True)
        except ValueError as exc:
            assert str(exc) == "ADMISSION_BUDGET_UNAVAILABLE"
            return "admission", False
        return "admission", True

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(48)))
    granted = sum(ok for _, ok in results) * 30
    market_used = sum(ok for lane, ok in results if lane == "market") * 30
    assert granted <= 550 and market_used <= 430
    assert ledger(connect)[1:] == (granted, granted)


@pytest.mark.parametrize("value", [-1, True, 1751])
def test_policy_rejects_invalid_scalar(value):
    with pytest.raises(ValueError, match="ADMISSION_BUDGET_POLICY_BOUNDS"):
        AdmissionBudgetPolicy(value)


def test_policy_reserve_requires_at_least_fifty_market_weight():
    policy = MarketPolicy(shared_weight_limit=2000, market_weight_limit=550)
    assert (
        AdmissionBudgetPolicy(500).validate_against(policy).admission_reserved_weight
        == 500
    )
    with pytest.raises(ValueError, match="ADMISSION_BUDGET_POLICY_RESERVE_INVALID"):
        AdmissionBudgetPolicy(501).validate_against(policy)
    assert "admission_reserved_weight" not in policy.values()


def test_policy_tightening_is_observed_by_existing_budget_and_ticket(budget):
    connect, clock, bucket = budget
    ticket = reserve(bucket, clock)
    configure(connect, reserved=501)
    before = ledger(connect)
    with pytest.raises(ValueError, match="ADMISSION_BUDGET_POLICY_RESERVE_INVALID"):
        take(bucket, ticket)
    assert ledger(connect) == before


@pytest.mark.parametrize("broken", ["invalid", "deleted"])
def test_admission_policy_failure_never_disables_management(budget, broken):
    connect, clock, bucket = budget
    key = StateKey(
        "BINANCE",
        "market-data",
        "SANDBOX",
        "FUTURES",
        "admission-budget-policy",
        "current",
    )
    if broken == "invalid":
        configure(connect, reserved=501)
    else:
        result = BusinessState(connect).change(
            key,
            expected_version=0,
            request_key="qa-disabled-share",
            reason="ISOLATED_QA",
            payload={},
            deleted=True,
        )
        assert result.code == "APPLIED"
    with pytest.raises(ValueError, match="ADMISSION_BUDGET_POLICY_"):
        reserve(bucket, clock)
    management = HostBudget(connect, environment="SANDBOX")
    assert management.permit(5)
    assert ledger(connect)[1:] == (5, 0)


def test_atomic_counter_failure_rolls_back_host_debit(budget):
    connect, _, _ = budget
    market = HostBudget(connect, environment="SANDBOX", lane="market")
    assert market.permit(5)
    before = ledger(connect)

    class BrokenLaneWrite:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, query, *args, **kwargs):
            if isinstance(query, str) and query.startswith(
                "UPDATE v2_public_market_budgets"
            ):
                raise RuntimeError("QA_COUNTER_WRITE_FAILED")
            return self.conn.execute(query, *args, **kwargs)

    @contextmanager
    def failing():
        with connect() as conn:
            yield BrokenLaneWrite(conn)

    with pytest.raises(RuntimeError, match="QA_COUNTER_WRITE_FAILED"):
        HostBudget(failing, environment="SANDBOX", lane="market").permit(5)
    assert ledger(connect) == before


def test_default_infinite_cooldown_is_decoded_inside_postgres(shared):
    budget = HostBudget(shared, environment="SANDBOX")
    assert budget.permit(1)
    with shared() as conn:
        assert conn.execute(
            "SELECT blocked_until::text FROM v2_egress_budgets WHERE host=%s", (HOST,)
        ).fetchone() == ("-infinity",)


@pytest.mark.parametrize("blocked", [None, 0, "false"])
def test_unknown_cooldown_is_not_treated_as_unblocked(blocked):
    class Reply:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

    class Connection:
        def execute(self, query, _params):
            if query.startswith("SELECT window_id"):
                return Reply((0, 0, 0, None))
            return Reply((1_800_000_001_000, blocked))

    budget = HostBudget(lambda: None, environment="SANDBOX")
    with pytest.raises(ValueError, match="EGRESS_LEDGER_INVALID"):
        budget._locked_state(Connection())


@pytest.mark.parametrize(
    "path,params,weight",
    [
        ("/fapi/v1/time", {}, 1),
        ("/fapi/v1/klines", {"symbol": TARGET}, 1),
        ("/futures/data/globalLongShortAccountRatio", {"symbol": TARGET}, 1),
        ("/fapi/v1/ticker/bookTicker", {"symbol": TARGET}, 2),
    ],
)
def test_ticket_cannot_pay_for_unplanned_public_get(budget, path, params, weight):
    connect, clock, bucket = budget
    ticket = reserve(bucket, clock)
    with pytest.raises(ValueError, match=INVALID):
        take(bucket, ticket, path=path, params=params, weight=weight)
    assert ledger(connect)[1:] == (27, 27)


def test_revalidation_after_connection_never_spends_weight_or_route_twice(budget):
    connect, clock, bucket = budget
    ticket = reserve(bucket, clock)
    assert take(bucket, ticket)
    before = ledger(connect)
    for _ in range(3):
        clock[0] += 1
        assert bucket.revalidate_context(
            ticket, account_id=ACCOUNT, environment="SANDBOX"
        )
    assert ledger(connect) == before
    with pytest.raises(ValueError, match=INVALID):
        take(bucket, ticket)
    assert take(bucket, ticket, path="/fapi/v3/positionRisk")
    assert ledger(connect) == before
    clock[0] += 60_000
    with pytest.raises(ValueError, match=UNAVAILABLE):
        bucket.revalidate_context(ticket, account_id=ACCOUNT, environment="SANDBOX")
    assert ledger(connect) == before


def test_admission_first_after_minute_rollover_does_not_reduce_market_twice(budget):
    connect, clock, bucket = budget
    configure(connect, reserved=120)
    market = HostBudget(connect, environment="SANDBOX", lane="market")
    spend(market, 430)
    clock[0] += 60_000
    for _ in range(4):
        reserve(bucket, clock, adaptive=True)
    spend(market, 430)
    assert ledger(connect)[1:] == (550, 550)
    with connect() as conn:
        assert conn.execute(
            "SELECT window_id,used_weight FROM v2_public_market_budgets WHERE scope=%s",
            (market.market_lane_scope,),
        ).fetchone() == (clock[0] // 60000, 430)


@pytest.mark.parametrize("first", ["market", "admission"])
def test_bidirectional_reserves_keep_each_minimum_and_share_idle_weight(budget, first):
    connect, clock, bucket = budget
    configure(connect, reserved=120, market_reserved=100)
    market = HostBudget(connect, environment="SANDBOX", lane="market")
    if first == "market":
        # Market can use more than its minimum100, but not admission's120.
        spend(market, 430)
        assert not market.permit(1)
        for _ in range(4):
            reserve(bucket, clock, adaptive=True)
    else:
        # Admission can use more than its minimum120, but not market's100.
        for _ in range(15):
            reserve(bucket, clock, adaptive=True)
        before = ledger(connect)
        with pytest.raises(ValueError, match=UNAVAILABLE):
            reserve(bucket, clock)
        assert ledger(connect) == before
        spend(market, 100)
    assert ledger(connect)[1:] == (550, 550)
    spend(HostBudget(connect, environment="SANDBOX"), 1450)
    assert ledger(connect)[1:] == (2000, 550)


def test_bidirectional_concurrency_never_spends_either_minimum(budget):
    connect, clock, bucket = budget
    configure(connect, reserved=120, market_reserved=100)

    def attempt(index):
        if index % 2:
            return "market", HostBudget(
                connect, environment="SANDBOX", lane="market"
            ).permit(30)
        try:
            reserve(bucket, clock, adaptive=True)
        except ValueError as exc:
            assert str(exc) == "ADMISSION_BUDGET_UNAVAILABLE"
            return "admission", False
        return "admission", True

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(64)))
    market = sum(allowed for lane, allowed in results if lane == "market") * 30
    admission = sum(allowed for lane, allowed in results if lane == "admission") * 30
    assert market <= 430 and admission <= 450 and market + admission <= 550
    assert ledger(connect)[1:] == (market + admission, market + admission)


@pytest.mark.parametrize("value", [-1, True, 1801])
def test_market_reserve_scalar_is_bounded(value):
    with pytest.raises(ValueError, match="ADMISSION_BUDGET_POLICY_BOUNDS"):
        AdmissionBudgetPolicy(market_reserved_weight=value)


def test_two_reserves_cannot_exceed_noncritical_cap():
    market = MarketPolicy(shared_weight_limit=2000, market_weight_limit=550)
    assert AdmissionBudgetPolicy(120, 100).validate_against(market)
    assert AdmissionBudgetPolicy(120, 430).validate_against(market)
    with pytest.raises(ValueError, match="ADMISSION_BUDGET_POLICY_RESERVE_INVALID"):
        AdmissionBudgetPolicy(120, 431).validate_against(market)


def test_old_single_field_policy_defaults_market_reserve_to_zero(budget):
    connect, _, _ = budget
    BusinessState(connect).change(
        StateKey(
            "BINANCE",
            "market-data",
            "SANDBOX",
            "FUTURES",
            "admission-budget-policy",
            "current",
        ),
        expected_version=0,
        request_key="qa-legacy-one-field",
        reason="ISOLATED_QA",
        payload={"admission_reserved_weight": 120},
    )
    assert AdmissionBudgetPolicy.read(connect, "SANDBOX").values() == {
        "admission_reserved_weight": 120,
        "market_reserved_weight": 0,
    }
