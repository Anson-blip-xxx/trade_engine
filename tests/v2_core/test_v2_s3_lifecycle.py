from copy import deepcopy

import pytest

from services.v2_s3_runtime import evaluate
from v2_core.breakout import advance_breakout
from v2_core.lifecycle import advance_events


def candidate(**changes):
    return {"symbol": "BTCUSDT", "type": "TREND_UP", "strength": 60, **changes}


def step(previous, candidates, time=100, **changes):
    return advance_events(
        previous,
        candidates,
        environment="SANDBOX",
        frame_id=f"frame:{time}",
        observed_at=time,
        covered={"BTCUSDT"},
        removed=set(),
        cooldown_ms=30,
        absence_ms=300,
        strength_delta=20,
        **changes,
    )


@pytest.mark.parametrize(
    "elapsed,delta,count", [(29, 19, 0), (30, 19, 1), (29, 20, 1), (1, -20, 1)]
)
def test_cooldown_and_strength_boundary(elapsed, delta, count):
    state, first = step({}, [candidate()])
    before = deepcopy(state)
    updated, events = step(state, [candidate(strength=60 + delta)], 100 + elapsed)
    assert state == before
    assert len(events) == count
    assert next(iter(updated.values()))["last_seen"] == 100 + elapsed
    if count:
        assert events[0]["features"]["state"] == "UPDATE"
        assert events[0]["features"]["since_ms"] == 100
        assert events[0]["event_id"] != first[0]["event_id"]


def test_absence_boundary_and_reactivation_identity():
    state, first = step({}, [candidate()])
    state, events = step(state, [], 400)
    assert events == []
    state, events = step(state, [], 401)
    assert state == {} and events[0]["signal"] == "EVENT_END"
    state, again = step(state, [candidate()], 402)
    assert again[0]["features"]["state"] == "ACTIVE"
    assert again[0]["features"]["episode_id"] != first[0]["features"]["episode_id"]


def test_missing_coverage_never_ends_but_explicit_removal_does():
    state, _ = step({}, [candidate()])
    args = {
        "environment": "SANDBOX",
        "frame_id": "later",
        "observed_at": 9999,
        "covered": set(),
        "cooldown_ms": 30,
        "absence_ms": 300,
        "strength_delta": 20,
    }
    same, events = advance_events(state, [], removed=set(), **args)
    assert same == state and events == []
    empty, events = advance_events(state, [], removed={"BTCUSDT"}, **args)
    assert empty == {} and events[0]["features"]["reason"] == "symbol_removed"


@pytest.mark.parametrize(
    "changes",
    [
        {"strength": True},
        {"strength": -1},
        {"strength": 101},
        {"symbol": "ETHUSDT"},
        {"state": "ACTIVE"},
        {"event_id": "x"},
        {"direction": "UNKNOWN"},
    ],
)
def test_invalid_candidates_fail_closed(changes):
    with pytest.raises(ValueError):
        step({}, [candidate(**changes)])


def test_duplicate_rejected_and_directions_independent():
    with pytest.raises(ValueError, match="duplicate"):
        step({}, [candidate(), candidate()])
    state, events = step(
        {},
        [
            candidate(type="FAILED_BREAKOUT", direction="HIGH"),
            candidate(type="FAILED_BREAKOUT", direction="LOW"),
        ],
    )
    assert len(state) == len(events) == 2


def bars(high="101", low="99", close="100"):
    return [{"h": high, "l": low, "c": close} for _ in range(3)]


def breakout(previous=None, *, raw=None, observed_at=100):
    return advance_breakout(
        previous,
        symbol="BTCUSDT",
        raw_4h=bars("100", "90", "95"),
        raw_15m=raw or bars(),
        observed_at=observed_at,
    )


@pytest.mark.parametrize(
    "direction,initial,rejected",
    [
        ("HIGH", bars(), bars("101", "99", "100")),
        ("LOW", bars("91", "89", "90"), bars("91", "89", "90")),
    ],
)
def test_failed_breakout_persists_and_emits(direction, initial, rejected):
    state, events = breakout(raw=initial)
    assert state["state"] == "BREAKING_" + direction and not events
    original = deepcopy(state)
    updated, events = breakout(state, raw=rejected, observed_at=101)
    assert state == original and updated["state"] == "IDLE"
    assert events[0]["direction"] == direction
    assert events[0]["type"] == "FAILED_BREAKOUT"


def test_breakout_timeout_precedes_rejection_and_strict_boundary():
    state, _ = breakout()
    assert breakout(state, observed_at=27000100)[1]
    assert breakout(state, observed_at=27000101) == ({"state": "IDLE"}, [])
    state, events = breakout(raw=bars("100.8", "99", "100"))
    assert state["state"] == "IDLE" and not events


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "0", "-1", "abc"])
def test_invalid_breakout_prices(bad):
    with pytest.raises(ValueError):
        breakout(raw=bars(bad))


def test_actual_detector_and_durable_breakout_callback():
    contexts = {
        "BTCUSDT": {
            "15m": {"chg": "6", "vol_ratio": "2"},
            "1h": {"chg": "9"},
            "4h": {"chg": "3"},
            "24h": {"chg": "6"},
        }
    }
    raw = {"BTCUSDT": {"4h": bars("100", "90", "95"), "15m": bars()}}
    events, states = evaluate(contexts, raw, {}, 100)
    assert {e["type"] for e in events} >= {"PULSE_UP", "TREND_UP"}
    assert states["BTCUSDT"]["state"] == "BREAKING_HIGH"
    events, states = evaluate(contexts, raw, states, 101)
    assert any(e["type"] == "FAILED_BREAKOUT" for e in events)


@pytest.mark.parametrize(
    "defect",
    ["missing_window", "missing_raw", "short_raw", "nan", "bool", "negative_volume"],
)
def test_incomplete_market_never_silently_becomes_no_candidates(defect):
    contexts = {
        "BTCUSDT": {
            period: {"chg": "0", "vol_ratio": "1"}
            for period in ("15m", "1h", "4h", "24h")
        }
    }
    raw = {"BTCUSDT": {"4h": bars(), "15m": bars()}}
    if defect == "missing_window":
        del contexts["BTCUSDT"]["4h"]
    elif defect == "missing_raw":
        raw = {}
    elif defect == "short_raw":
        raw["BTCUSDT"]["15m"] = bars()[:2]
    elif defect in ("nan", "bool"):
        contexts["BTCUSDT"]["15m"]["chg"] = "NaN" if defect == "nan" else True
    else:
        contexts["BTCUSDT"]["15m"]["vol_ratio"] = "-1"
    with pytest.raises(ValueError):
        evaluate(contexts, raw, {}, 100)


def test_breakout_continuation_and_future_bar_are_not_failure():
    state, _ = breakout()
    assert breakout(state, raw=bars("102", "101", "102"), observed_at=101) == (
        {"state": "IDLE"},
        [],
    )
    raw = bars()
    raw[0]["t"] = 101
    with pytest.raises(ValueError, match="future"):
        breakout(raw=raw)


def test_suppressed_candidates_still_refresh_last_seen():
    state, _ = step({}, [candidate()])
    state, events = step(state, [candidate()], 129)
    assert events == []
    state, events = step(state, [], 401)
    assert state and events == []
    assert step(state, [], 430)[1][0]["signal"] == "EVENT_END"
