"""Offline regression checks: no network, business writes or policy promotion."""

import copy
import json
from dataclasses import dataclass

import pytest

from scripts.research_v2_trade_cycle_readonly import (
    MAX_PAIRS,
    MINUTE,
    active_scope,
    amount,
    collect,
    day_ms,
    pair_cycles,
    replay,
    settled_rows,
)
from v2_core.reentry import ReentryPolicy


@dataclass(frozen=True)
class CyclePolicyFixture:
    confirmation_bars: int = 2
    max_trigger_age_bars: int = 0


def trade(
    identity,
    *,
    opened=110 * MINUTE,
    closed=130 * MINUTE,
    net="-2",
    side="BUY",
    symbol="APRUSDT",
):
    return {
        "id": identity,
        "symbol": symbol,
        "side": side,
        "opened_ms": opened,
        "closed_ms": closed,
        "decided_ms": opened - 1000,
        "close_known_ms": closed + 1000,
        "settled_ms": closed + 2000,
        "settlement_state": "SETTLED",
        "net_pnl": net,
        "maintenance": False,
    }


def fixture():
    previous = trade(
        "SECRET_PRIOR_EPISODE", opened=70 * MINUTE, closed=100 * MINUTE, net="3"
    )
    current = trade(
        "SECRET_CURRENT_EPISODE", opened=120 * MINUTE + 1000, closed=140 * MINUTE
    )
    inputs = {}
    for row in (previous, current):
        inputs[row["id"]] = {
            "id": row["id"],
            "symbol": row["symbol"],
            "side": row["side"],
            "as_of_ms": 120 * MINUTE,
            "s3_observed_ms": 120 * MINUTE,
            "signal_observed_ms": 118 * MINUTE,
            "signal_known_ms": 118 * MINUTE + 10,
            "exit_ms": row["closed_ms"],
            "exit_known_ms": row["close_known_ms"],
            "exit_price": "100",
            "exit_orders": 1,
            "exit_reason": "PEAK_DRAWDOWN",
        }
    return {
        "pairs": [{"current": current, "previous": previous, "pair_error": None}],
        "inputs": inputs,
        "coverage": {},
        "start_ms": 0,
        "end_ms": 200 * MINUTE,
        "snapshot_ms": 200 * MINUTE,
    }


def bars(symbol, start, end):
    assert symbol == "APRUSDT"
    return [
        {"t": t, "o": "100", "h": "101", "l": "99", "c": "100", "v": "10", "tbv": "5"}
        for t in reversed(range(start, end, MINUTE))
    ]


def run(snapshot=None, *, read=bars, assess=None, clock=lambda: 0):
    return replay(
        fixture() if snapshot is None else snapshot,
        read_bars=read,
        assess=assess
        or (lambda **kwargs: {"status": "WAIT", "reason": "NO_NEW_STRUCTURE"}),
        policy=ReentryPolicy(),
        cycle_policy=CyclePolicyFixture(),
        clock=clock,
    )


def test_scope_free_output_and_all_outcomes_preserved():
    report = run()
    assert report["groups"]["WAIT"]["actual_net_pnl_usdt"] == "-2"
    assert report["groups"]["WAIT"]["losses"] == 1
    assert report["cases"][0]["status"] == "WAIT"
    assert "SECRET" not in json.dumps(report)
    assert report["archive_arrival_at_decision_proven"] is False
    assert report["database_writes"] is False
    assert report["orders_authorized"] is False


def test_original_context_times_and_closed_bars_are_used():
    received = []
    report = run(
        assess=lambda **kwargs: (
            received.append(kwargs) or {"status": "READY", "reason": "RESET_CONFIRMED"}
        )
    )
    inputs = received[0]
    assert inputs["as_of_ms"] == 120 * MINUTE
    assert inputs["bar_cutoff_ms"] == 120 * MINUTE
    assert inputs["signal_observed_ms"] == 118 * MINUTE
    assert inputs["bars"][-1].t + MINUTE == 120 * MINUTE
    assert inputs["previous"]["exit_net_pnl"] == "3"
    assert report["groups"]["READY"]["count"] == 1


def test_future_settlement_pnl_not_passed_to_gate():
    snapshot, received = fixture(), []
    snapshot["pairs"][0]["previous"]["settled_ms"] = 121 * MINUTE
    run(
        snapshot,
        assess=lambda **kwargs: (
            received.append(kwargs) or {"status": "WAIT", "reason": "CHECK"}
        ),
    )
    assert received[0]["previous"]["exit_net_pnl"] is None


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("signal_known_ms", 121 * MINUTE, "INPUT_NOT_KNOWN_AT_ORIGINAL_CONTEXT"),
        ("signal_observed_ms", 121 * MINUTE, "INPUT_NOT_KNOWN_AT_ORIGINAL_CONTEXT"),
        ("s3_observed_ms", 121 * MINUTE, "INPUT_NOT_KNOWN_AT_ORIGINAL_CONTEXT"),
        ("as_of_ms", 122 * MINUTE, "INPUT_NOT_KNOWN_AT_ORIGINAL_CONTEXT"),
        ("as_of_ms", None, "INVALID_OR_MISSING_FROZEN_INPUT"),
        ("symbol", "BTCUSDT", "DECISION_IDENTITY_MISMATCH"),
    ],
)
def test_bad_current_input_never_reads_archive(field, value, reason):
    snapshot = fixture()
    snapshot["inputs"]["SECRET_CURRENT_EPISODE"][field] = value
    report = run(snapshot, read=lambda *_: pytest.fail("unexpected archive read"))
    assert report["cases"][0]["reason"] == reason
    assert report["cases"][0]["status"] == "UNKNOWN"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("exit_known_ms", 121 * MINUTE, "PRIOR_CLOSE_NOT_KNOWN_AT_ORIGINAL_CONTEXT"),
        ("exit_ms", 99 * MINUTE, "PRIOR_CLOSE_NOT_KNOWN_AT_ORIGINAL_CONTEXT"),
        ("exit_orders", 2, "PRIOR_TERMINAL_PRICE_AMBIGUOUS"),
        ("exit_price", "0", "PRIOR_TERMINAL_PRICE_AMBIGUOUS"),
        ("symbol", "BTCUSDT", "PRIOR_IDENTITY_MISMATCH"),
    ],
)
def test_bad_prior_input_never_reads_archive(field, value, reason):
    snapshot = fixture()
    snapshot["inputs"]["SECRET_PRIOR_EPISODE"][field] = value
    report = run(snapshot, read=lambda *_: pytest.fail("unexpected archive read"))
    assert report["cases"][0]["reason"] == reason


def test_later_any_close_order_update_is_unknown():
    snapshot = fixture()
    snapshot["pairs"][0]["previous"]["close_known_ms"] = 150 * MINUTE
    result = run(snapshot)
    assert result["cases"][0]["reason"] == "PRIOR_CLOSE_NOT_KNOWN_AT_ORIGINAL_CONTEXT"


def test_long_anchor_reads_at_most_720_minutes_not_time_based_ready():
    snapshot, calls = fixture(), []
    current = snapshot["pairs"][0]["current"]
    current.update(opened_ms=1000 * MINUTE + 1000, closed_ms=1020 * MINUTE)
    snapshot["inputs"][current["id"]].update(
        as_of_ms=1000 * MINUTE,
        s3_observed_ms=1000 * MINUTE,
        signal_observed_ms=998 * MINUTE,
        signal_known_ms=998 * MINUTE,
    )
    report = run(snapshot, read=lambda s, a, b: calls.append((a, b)) or bars(s, a, b))
    assert calls == [(280 * MINUTE, 1000 * MINUTE)]
    assert report["cases"][0]["anchor_truncated"] is True
    assert report["cases"][0]["status"] == "WAIT"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda values: values[:-1],
        lambda values: values + [values[-1]],
        lambda values: [{**value, "t": value["t"] + MINUTE} for value in values],
        lambda values: [{**value, "c": "NaN"} for value in values],
    ],
)
def test_missing_duplicate_future_or_corrupt_candle_is_unknown(mutate):
    report = run(read=lambda *args: mutate(bars(*args)))
    assert report["cases"][0]["reason"] == "ARCHIVE_INCOMPLETE_OR_UNAVAILABLE"


def test_archive_error_text_not_exposed():
    def read(*args):
        raise RuntimeError("secret-password@example.com")

    report = run(read=read)
    assert "secret-password" not in json.dumps(report)
    assert report["cases"][0]["status"] == "UNKNOWN"


def test_budget_exhaustion_keeps_case_unknown_without_query():
    clock = iter([0, 181])
    report = run(
        clock=lambda: next(clock), read=lambda *_: pytest.fail("budget ignored")
    )
    assert report["cases"][0]["reason"] == "ARCHIVE_TOTAL_BUDGET_EXHAUSTED"


def test_pairing_is_by_latest_final_close_not_pnl_or_side():
    rows = [
        trade("a", opened=10, closed=20, net="100"),
        trade("b", opened=30, closed=40, side="SELL", net="-100"),
        trade("c", opened=50, closed=60, net="2"),
    ]
    pairs, coverage = pair_cycles(rows)
    assert [(p["previous"]["id"], p["current"]["id"]) for p in pairs] == [
        ("a", "b"),
        ("b", "c"),
    ]
    assert coverage["eligible_pairs"] == 2


def test_pairing_tie_is_explicit_not_arbitrary():
    rows = [
        trade("a", opened=10, closed=40),
        trade("b", opened=20, closed=40),
        trade("c", opened=50, closed=60),
    ]
    pairs, _ = pair_cycles(rows)
    assert len(pairs) == 1
    assert pairs[0]["previous"] is None
    assert pairs[0]["pair_error"] == "PRIOR_CLOSE_AMBIGUOUS"


def test_pairing_does_not_borrow_other_symbols():
    pairs, coverage = pair_cycles(
        [
            trade("a", opened=10, closed=20),
            trade("b", opened=30, closed=40, symbol="JCTUSDT"),
        ]
    )
    assert pairs == []
    assert coverage["unpaired"]["NO_PRIOR_FLAT_IN_BOUNDED_SAMPLE"] == 2


def test_overlapping_unclosed_position_not_replaced_by_old_flat():
    rows = [
        trade("old", opened=10, closed=20),
        trade("overlap", opened=30, closed=70),
        trade("new", opened=50, closed=80),
    ]
    pairs, _ = pair_cycles(rows)
    assert pairs[-1]["pair_error"] == "PREVIOUS_EPISODE_STILL_OPEN"


def test_core_failure_is_local_unknown_and_does_not_leak():
    def assess(**kwargs):
        raise RuntimeError("private-account-password")

    result = run(assess=assess)
    assert result["cases"][0]["reason"] == "ASSESSMENT_UNAVAILABLE_OR_INVALID"
    assert "private-account-password" not in json.dumps(result)


def test_pair_cap_is_chronological_and_explicit():
    rows = [trade(str(i), opened=10 * i + 1, closed=10 * i + 5) for i in range(140)]
    pairs, coverage = pair_cycles(rows)
    assert len(pairs) == MAX_PAIRS
    assert coverage["pairs_truncated"] is True
    assert pairs[-1]["current"]["id"] == str(MAX_PAIRS)


def test_all_settled_outcomes_including_zero_and_bad_time_rejected():
    rows = [trade("a", net="0"), trade("b", net="5"), trade("c", net="-5")]
    rows.append({**trade("bad"), "opened_ms": 1})
    rows.append({**trade("open"), "settlement_state": "OPEN_OR_PARTIAL_EXIT"})
    accepted, excluded = settled_rows(rows, start_ms=0, end_ms=200 * MINUTE)
    assert len(accepted) == 3
    assert excluded == {"INVALID_SETTLEMENT_OR_TIMING": 1, "NOT_STRICTLY_SETTLED": 1}


def test_reports_do_not_mutate_frozen_inputs():
    snapshot = fixture()
    before = copy.deepcopy(snapshot)
    run(snapshot)
    assert snapshot == before


def test_utc8_boundaries_and_decimal_validation():
    assert day_ms("2026-10-21") == 1792512000000
    for invalid in ("NaN", "Infinity", "1e100", None, 1):
        with pytest.raises((ValueError, ArithmeticError)):
            amount(invalid)


def test_invalid_assessment_cannot_smuggle_secrets_or_authorize_orders():
    report = run(
        assess=lambda **_: {
            "status": "READY",
            "reason": "password: x",
            "account_id": "SECRET",
        }
    )
    assert report["cases"][0]["reason"] == "ASSESSMENT_CONTRACT_INVALID"
    assert report["cases"][0]["status"] == "UNKNOWN"
    assert "password" not in json.dumps(report)


@pytest.mark.parametrize("net,expected", [("3", "READY"), ("-3", "WAIT")])
def test_actual_core_contract_with_positive_and_negative_prior(net, expected):
    from v2_core.trade_cycle import CyclePolicy, assess_trade_cycle

    snapshot = fixture()
    previous = snapshot["pairs"][0]["previous"]
    current = snapshot["pairs"][0]["current"]
    previous.update(
        opened_ms=MINUTE,
        closed_ms=5 * MINUTE,
        close_known_ms=5 * MINUTE,
        settled_ms=5 * MINUTE + 10,
        net_pnl=net,
    )
    current.update(opened_ms=7 * MINUTE + 2000)
    snapshot["inputs"][previous["id"]].update(
        exit_ms=5 * MINUTE, exit_known_ms=5 * MINUTE
    )
    snapshot["inputs"][current["id"]].update(
        as_of_ms=7 * MINUTE + 1000,
        s3_observed_ms=7 * MINUTE,
        signal_observed_ms=7 * MINUTE,
        signal_known_ms=7 * MINUTE + 10,
    )

    def closed_breakout(symbol, start, end):
        values = bars(symbol, start, end)
        values[1].update(h="103", c="102", v="20")
        values[0].update(o="102", h="103", l="101", c="102.5", v="20")
        return values

    result = replay(
        snapshot,
        read_bars=closed_breakout,
        assess=assess_trade_cycle,
        policy=ReentryPolicy(anchor_bars=3, atr_period=2, volume_period=3),
        cycle_policy=CyclePolicy(),
    )
    assert result["cases"][0]["status"] == expected
    assert result["cases"][0]["orders_authorized"] is False


@pytest.mark.parametrize(
    "read_only,isolation", [("off", "repeatable read"), ("on", "read committed")]
)
def test_collection_requires_frozen_readonly_snapshot(read_only, isolation):
    class Result:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    class Connection:
        def execute(self, sql):
            assert sql.startswith("SHOW ")
            return Result(
                read_only if sql == "SHOW transaction_read_only" else isolation
            )

    with pytest.raises(ValueError, match="READ_ONLY_SNAPSHOT_REQUIRED"):
        collect(Connection(), start_ms=0, end_ms=1, as_of_ms=1)


@pytest.mark.parametrize("rows", [[], [("BINANCE", "a", "SANDBOX", "FUTURES")] * 2])
def test_scope_ambiguous_or_absent_never_falls_back_to_any_account(rows):
    class Connection:
        def execute(self, sql):
            assert "a.tenant_id=r.tenant_id" in sql
            assert "r.phase='ACTIVE'" in sql
            assert "LIMIT 2" in sql
            return self

        def fetchall(self):
            return rows

    with pytest.raises(ValueError, match="UNIQUE_ACTIVE_SANDBOX_REQUIRED"):
        active_scope(Connection())


@pytest.mark.parametrize(
    "previous_side,net,gap_minutes,expected_group",
    [
        ("BUY", "3", 5, "same_side_prior_win_within_5m"),
        ("SELL", "-3", 60, "opposite_side_within_60m"),
        ("BUY", "-3", 5, None),
        ("BUY", "3", 6, None),
        ("SELL", "3", 61, None),
    ],
)
def test_subgroups_use_actual_times_prior_net_and_preserve_original_outcome(
    previous_side, net, gap_minutes, expected_group
):
    from scripts.research_v2_trade_cycle_readonly import subgroup_report

    snapshot = fixture()
    previous, current = (
        snapshot["pairs"][0]["previous"],
        snapshot["pairs"][0]["current"],
    )
    previous.update(side=previous_side, net_pnl=net)
    current["opened_ms"] = previous["closed_ms"] + gap_minutes * MINUTE
    groups = subgroup_report(
        snapshot, [{"status": "WAIT", "actual_net_pnl_usdt": "-2"}]
    )
    for name, group in groups.items():
        assert group["overall"]["count"] == (1 if name == expected_group else 0)
        if name == expected_group:
            assert group["states"]["WAIT"]["actual_net_pnl_usdt"] == "-2"
            assert group["states"]["WAIT"]["losses"] == 1
            assert group["states"]["READY"]["count"] == 0


def test_sample_digest_identifies_pair_or_settlement_changes_without_ids():
    snapshot = fixture()
    first = run(snapshot)
    assert len(first["sample_digest"]) == 64
    snapshot["pairs"][0]["current"]["net_pnl"] = "-2.1"
    assert run(snapshot)["sample_digest"] != first["sample_digest"]
