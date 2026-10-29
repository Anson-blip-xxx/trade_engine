from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal

import pytest

from services.v2_entry_structure import EntryStructureContext, prior_close
from v2_core.account_risk import AccountScope
from v2_core.entry_controls import Controls, resolve
from v2_core.entry_opportunities import seal, validate
from v2_core.evidence import canonical, digest
from v2_core.reentry import ReentryPolicy

SCOPE = AccountScope("BINANCE", "test-structure", "SANDBOX", "FUTURES")
NOW = 27 * 60000 + 1000
PREVIOUS = "c2501c0e-7296-4796-aa6a-f6abb236f96c"


def row(**changes):
    values = {
        "identity": PREVIOUS,
        "side": "BUY",
        "status": "SETTLED",
        "oq": Decimal(1),
        "cq": Decimal(1),
        "closed": 25 * 60000,
        "known": 25 * 60000,
        "price": "100",
        "count": 1,
        "reason": "PEAK_DRAWDOWN",
        "pending": False,
        "net": "1",
    }
    values.update(changes)
    return tuple(values.values())


class Connection:
    def __init__(self, previous):
        self.previous, self.statements = previous, []

    def execute(self, query, params=None):
        self.statements.append((query, params))
        return self

    def fetchone(self):
        return self.previous


def setup(
    *,
    previous="default",
    mode="ENFORCE",
    strategy="S6",
    now=NOW,
    mutate_bars=None,
    changes=None,
):
    connection = Connection(row() if previous == "default" else previous)

    @contextmanager
    def connect():
        yield connection

    values = resolve({"reentry.mode": mode, **(changes or {})})
    controls = Controls(1, values, digest(canonical(values)))
    context = {
        "assembled_at": now - 100,
        "valid_until_ms": now + 60000,
        "sources": {"s3": {"observed_at": now}},
        "account_evidence": {"entry_controls": controls.snapshot()},
    }
    calls = []

    def read_bars(target, start, end):
        calls.append((target, start, end))
        bars = []
        for t in range(start, end, 60000):
            bar = {
                "t": t,
                "o": "100",
                "h": "101",
                "l": "99",
                "c": "100",
                "v": "10",
                "tbv": "5",
            }
            if t == 25 * 60000:
                bar.update(h="102", c="101.5", v="20")
            if t == 26 * 60000:
                bar.update(o="101.5", h="111", c="110", v="20")
            bars.append(bar)
        bars.reverse()
        return mutate_bars(bars) if mutate_bars else bars

    signal = {
        "symbol": "BTCUSDT",
        "observed_at": now - 10,
        "expires_at_ms": now + 60000,
    }
    wrapper = EntryStructureContext(
        connect,
        lambda _: context,
        scope=SCOPE,
        strategy=strategy,
        read_bars=read_bars,
        clock_ms=lambda: now,
        archive_clock_ms=lambda: now,
        read_controls=lambda *_: controls,
    )
    return wrapper, signal, context, calls, connection


def check(proof, **overrides):
    args = {
        "scope": asdict(SCOPE),
        "target": "BTCUSDT",
        "side": proof["side"],
        "observed": proof["signal_observed_ms"],
        "now": proof["finished_at_ms"],
        "expected_control_digest": proof["control_digest"],
    }
    args.update(overrides)
    return validate(proof, **args)


def test_chasing_buy_enforced_from_fresh_archive_and_actual_settlement():
    wrapper, signal, original, calls, connection = setup()
    output = wrapper(signal)
    proof = output["entry_structure"]
    assert proof["assessment"]["reason"] == "STRUCTURE_CHASE_TOO_FAR"
    assert check(proof) == "REENTRY_STRUCTURE_CHASE_TOO_FAR"
    assert proof["previous_episode"] == PREVIOUS
    assert proof["execution_authorized"] is False
    assert proof["historical_replay_compatible"] is False
    assert proof["archive_read_finished_ms"] <= proof["as_of_ms"]
    assert len(proof["archive_bar_digests"]) == 24
    assert output["assembled_at"] == NOW
    assert "entry_structure" not in original
    assert len(calls) == 1
    assert all(
        "INSERT " not in q and "UPDATE " not in q for q, _ in connection.statements
    )


@pytest.mark.parametrize(
    "rejection", [{"market_only": True}, {"universe_admission": {"eligible": False}}]
)
def test_fast_market_rejection_never_reads_controls_or_research(rejection):
    wrapper, signal, original, calls, connection = setup()
    original.update(rejection)
    wrapper.read_controls = lambda *_: pytest.fail("no rejected-signal control IO")
    assert wrapper(signal) == original
    assert calls == [] and connection.statements == []


def test_shadow_records_identical_verdict_without_veto():
    wrapper, signal, *_ = setup(mode="SHADOW")
    proof = wrapper(signal)["entry_structure"]
    assert proof["assessment"]["reason"] == "STRUCTURE_CHASE_TOO_FAR"
    assert check(proof) is None


def test_off_freezes_controls_without_history_io():
    wrapper, signal, _, calls, connection = setup(mode="OFF")
    output = wrapper(signal)
    assert "entry_structure" not in output
    assert output["entry_controls"]["values"]["reentry.mode"] == "OFF"
    assert not calls and not connection.statements


@pytest.mark.parametrize(
    "previous,reason",
    [
        (None, "NO_PREVIOUS_FILLED_EPISODE"),
        (row(side="SELL"), "PREVIOUS_OPPOSITE_SIDE"),
        (row(status="ACTIVE"), "PREVIOUS_EPISODE_UNRESOLVED"),
        (row(oq=Decimal(2)), "PREVIOUS_EPISODE_UNRESOLVED"),
        (row(pending=True), "PREVIOUS_EPISODE_UNRESOLVED"),
        (row(closed=NOW + 1), "PREVIOUS_EPISODE_UNRESOLVED"),
        (row(count=2), "PREVIOUS_CLOSE_PRICE_AMBIGUOUS"),
    ],
)
def test_missing_ambiguous_other_side_never_claims_ready_or_veto(previous, reason):
    wrapper, signal, _, calls, _ = setup(previous=previous)
    proof = wrapper(signal)["entry_structure"]
    assert proof["assessment"]["reason"] == reason
    assert proof["assessment"]["status"] in {"UNKNOWN", "NOT_APPLICABLE"}
    assert check(proof) is None and not calls


def test_unknown_and_wait_not_blanket_blocked():
    for previous in (row(reason="UNSUPPORTED"), row(reason="EMERGENCY_LOSS")):
        wrapper, signal, *_ = setup(previous=previous)
        proof = wrapper(signal)["entry_structure"]
        assert proof["assessment"]["status"] in {"UNKNOWN", "WAIT"}
        assert check(proof) is None


def test_cold_history_over_budget_does_not_call_archive():
    wrapper, signal, _, calls, _ = setup(now=800 * 60000 + 1000)
    proof = wrapper(signal)["entry_structure"]
    assert proof["assessment"] == {
        "status": "UNKNOWN",
        "reason": "REENTRY_HISTORY_BUDGET_EXCEEDED",
    }
    assert not calls and check(proof) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda bars: bars[:-1],
        lambda bars: [{**bars[0], "t": bars[0]["t"] + 60000}, *bars[1:]],
        lambda bars: [bars[0], bars[0], *bars[2:]],
        lambda bars: list(reversed(bars)),
    ],
)
def test_incomplete_unclosed_duplicate_and_unsorted_bars_are_unknown(mutate):
    wrapper, signal, *_ = setup(mutate_bars=mutate)
    proof = wrapper(signal)["entry_structure"]
    assert proof["assessment"]["status"] == "UNKNOWN"
    assert check(proof) is None


def test_archive_fault_sanitized_without_permission():
    wrapper, signal, *_ = setup()
    wrapper.read_bars = lambda *_: (_ for _ in ()).throw(
        RuntimeError("secret database details")
    )
    proof = wrapper(signal)["entry_structure"]
    assert proof["assessment"]["status"] == "UNKNOWN"
    assert "secret" not in str(proof)


def test_context_control_change_reproposes_before_read():
    wrapper, signal, context, calls, _ = setup()
    context["account_evidence"]["entry_controls"]["version"] += 1
    with pytest.raises(ValueError, match="ENTRY_CONTROLS_CHANGED_REPROPOSE"):
        wrapper(signal)
    assert not calls


def test_expired_market_context_or_read_cannot_create_fresh_proof():
    wrapper, signal, context, calls, _ = setup()
    context["sources"]["s3"]["observed_at"] -= 120000
    proof = wrapper(signal)["entry_structure"]
    assert proof["assessment"] == {
        "status": "UNKNOWN",
        "reason": "REENTRY_MARKET_STALE",
    }
    assert check(proof) is None
    assert not calls
    wrapper, signal, *_ = setup()
    times = iter([NOW, NOW, NOW + 31000, NOW + 31000, NOW + 31000])
    wrapper.clock = lambda: next(times)
    with pytest.raises(ValueError, match="EXPIRED"):
        wrapper(signal)


def test_proof_is_bound_to_account_symbol_side_signal_policy_and_time():
    wrapper, signal, *_ = setup()
    proof = wrapper(signal)["entry_structure"]
    for changes in (
        {"scope": {**asdict(SCOPE), "account_id": "other"}},
        {"target": "ETHUSDT"},
        {"side": "SELL"},
        {"observed": signal["observed_at"] - 1},
        {"expected_control_digest": "x"},
        {"now": proof["valid_until_ms"]},
    ):
        with pytest.raises(ValueError, match="INVALID_OR_STALE"):
            check(proof, **changes)
    tampered = deepcopy(proof)
    tampered["mode"] = "SHADOW"
    with pytest.raises(ValueError, match="INVALID_OR_STALE"):
        check(tampered)


def test_sealed_missing_archive_evidence_is_not_accepted():
    wrapper, signal, *_ = setup()
    proof = wrapper(signal)["entry_structure"]
    proof["archive_read_finished_ms"] = NOW + 1
    with pytest.raises(ValueError, match="ARCHIVE_EVIDENCE_INVALID"):
        check(seal(proof))


def test_sell_not_enforced_in_buy_only_canary():
    wrapper, signal, *_ = setup()
    proof = wrapper(signal)["entry_structure"]
    proof.update(side="SELL", strategy="S8")
    proof["previous_exit"]["side"] = "SELL"
    assert check(seal(proof)) is None


def test_prior_query_exact_scope_ignores_shadow_claims_and_unfilled_opens():
    connection = Connection(row())
    previous, issue = prior_close(connection, SCOPE, "BTCUSDT", NOW)
    assert previous["previous_episode"] == PREVIOUS and issue is None
    query, params = connection.statements[0]
    assert params == (*asdict(SCOPE).values(), "BTCUSDT")
    assert "f.oq>0" in query and "shadow" not in query


def test_live_constructor_rejected():
    with pytest.raises(ValueError, match="SCOPE_REQUIRED"):
        EntryStructureContext(
            None,
            None,
            scope=AccountScope("BINANCE", "other", "LIVE", "FUTURES"),
            strategy="S6",
            read_bars=None,
            clock_ms=None,
        )


@pytest.mark.parametrize(
    "timeframe,minute", [(5, i) for i in range(5)] + [(15, i) for i in range(15)]
)
def test_fresh_market_not_expired_between_higher_timeframe_closures(timeframe, minute):
    now = (timeframe * 30 + minute) * 60000 + 1000
    previous = row(closed=25 * timeframe * 60000, known=25 * timeframe * 60000)
    policy = asdict(ReentryPolicy(timeframe_minutes=timeframe))
    wrapper, signal, _, calls, _ = setup(
        previous=previous, now=now, changes={"reentry.policy": policy}
    )
    output = wrapper(signal)
    proof = output["entry_structure"]
    assert proof["market_cutoff_ms"] == timeframe * 30 * 60000
    assert proof["market_observed_at_ms"] == now
    assert proof["assessment"]["reason"] == "STRUCTURE_NOT_REARMED"
    assert check(proof) is None
    assert calls[0][2] <= now
    assert all(
        item["open_ms"] + 60000 <= proof["market_cutoff_ms"]
        for item in proof["archive_bar_digests"]
    )


@pytest.mark.parametrize("bad", [None, "bad", {"t": 1.1}, {"t": 0, "price": 1.1}])
def test_malformed_archive_data_does_not_crash_evidence_freeze(bad):
    wrapper, signal, *_ = setup(mutate_bars=lambda bars: [bad, *bars[1:]])
    proof = wrapper(signal)["entry_structure"]
    assert proof["assessment"]["status"] == "UNKNOWN"
    assert proof["archive_bar_digests"] == []
    assert check(proof) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "SHADOW"),
        ("control_version", 7),
        ("enforce_sides", ["SELL"]),
        ("enforce_reasons", []),
        ("policy", asdict(ReentryPolicy(max_chase_atr="2"))),
    ],
)
def test_resealed_proof_must_match_actual_controls_not_just_digest(field, value):
    wrapper, signal, *_ = setup()
    output = wrapper(signal)
    proof = deepcopy(output["entry_structure"])
    proof[field] = value
    with pytest.raises(ValueError, match="CONTROL_MISMATCH"):
        check(seal(proof), expected_controls=output["entry_controls"])


def test_independent_structure_freshness_does_not_tighten_base_context():
    wrapper, signal, context, calls, _ = setup()
    context["sources"]["s3"]["observed_at"] = NOW - 91000
    output = wrapper(signal)
    proof = output["entry_structure"]
    assert proof["assessment"]["status"] == "UNKNOWN"
    assert output["valid_until_ms"] == NOW + 30000
    assert check(proof, expected_controls=output["entry_controls"]) is None
    assert not calls


def test_base_context_expiry_is_still_enforced():
    wrapper, signal, context, *_ = setup()
    context["valid_until_ms"] = NOW
    with pytest.raises(ValueError, match="EXPIRED"):
        wrapper(signal)


def test_complete_expected_controls_can_enforce_fresh_chase():
    wrapper, signal, *_ = setup()
    output = wrapper(signal)
    assert (
        check(output["entry_structure"], expected_controls=output["entry_controls"])
        == "REENTRY_STRUCTURE_CHASE_TOO_FAR"
    )


def test_historical_decision_clock_cannot_backdate_present_archive_availability():
    wrapper, signal, *_ = setup()
    wrapper.archive_clock = lambda: NOW + 86400000
    proof = wrapper(signal)["entry_structure"]
    assert proof["assessment"] == {
        "status": "UNKNOWN",
        "reason": "REENTRY_ARCHIVE_AVAILABILITY_UNPROVEN",
    }
    assert check(proof) is None
