"""Candidate-local trade-cycle gating; legacy frozen proofs remain replayable."""

from copy import deepcopy
from dataclasses import asdict

import pytest
from test_v2_entry_structure import SCOPE, check, row, setup

from v2_core.entry_controls import from_snapshot, resolve
from v2_core.entry_opportunities import seal
from v2_core.evidence import canonical, digest


def cycle(**changes):
    return {
        "reentry.gate_scope": "TRADE_CYCLE",
        "reentry.enforce_sides": ["BUY", "SELL"],
        **changes,
    }


def flat(bars):
    return [
        {**b, "o": "100", "h": "101", "l": "99", "c": "100", "v": "10"} for b in bars
    ]


def test_opposite_direction_is_evaluated_not_marked_not_applicable():
    wrapper, signal, _, calls, _ = setup(
        previous=row(side="SELL"), changes=cycle(), mutate_bars=flat
    )
    output = wrapper(signal)
    proof = output["entry_structure"]
    assert calls
    assert proof["previous_exit"]["side"] == "SELL"
    assert proof["assessment"]["status"] == "WAIT"
    assert check(proof, expected_controls=output["entry_controls"]).startswith(
        "REENTRY_"
    )


def test_same_side_without_new_structure_is_declined_not_exception():
    wrapper, signal, *_ = setup(changes=cycle(), mutate_bars=flat)
    output = wrapper(signal)
    assert output["entry_structure"]["assessment"]["status"] == "WAIT"
    assert check(
        output["entry_structure"], expected_controls=output["entry_controls"]
    ).startswith("REENTRY_")


def test_first_entry_does_not_require_fictitious_exit_or_archive():
    wrapper, signal, _, calls, _ = setup(previous=None, changes=cycle())
    proof = wrapper(signal)["entry_structure"]
    assert not calls
    assert proof["assessment"] == {
        "status": "NOT_APPLICABLE",
        "reason": "NO_PREVIOUS_FILLED_EPISODE",
    }
    assert check(proof) is None


@pytest.mark.parametrize("mode", ["SHADOW", "ENFORCE"])
def test_unknown_is_local_rejection_only_when_enforced(mode):
    wrapper, signal, *_ = setup(changes=cycle(), mode=mode)
    wrapper.read_bars = lambda *_: (_ for _ in ()).throw(
        RuntimeError("private details")
    )
    proof = wrapper(signal)["entry_structure"]
    assert proof["assessment"]["status"] == "UNKNOWN"
    assert "private details" not in str(proof)
    assert (check(proof) is not None) == (mode == "ENFORCE")


def test_long_exit_uses_bounded_new_base_not_timeout_unlock_or_permanent_budget_failure():
    wrapper, signal, _, calls, _ = setup(
        now=2000 * 60000 + 1000, changes=cycle(), mutate_bars=flat
    )
    proof = wrapper(signal)["entry_structure"]
    assert len(calls) == 1 and calls[0][2] - calls[0][1] == 720 * 60000
    assert proof["assessment"]["anchor_basis"] == "POST_EXIT_ROLLING_BASE"
    assert proof["assessment"]["status"] == "WAIT"
    assert proof["assessment"]["reason"] != "REENTRY_HISTORY_BUDGET_EXCEEDED"
    assert check(proof).startswith("REENTRY_")


def test_stale_market_cannot_bypass_full_cycle_gate():
    wrapper, signal, context, calls, _ = setup(changes=cycle())
    context["sources"]["s3"]["observed_at"] -= 91000
    proof = wrapper(signal)["entry_structure"]
    assert not calls
    assert check(proof) == "REENTRY_MARKET_STALE"


def test_opposite_side_cannot_be_resealed_as_first_entry():
    wrapper, signal, *_ = setup(
        previous=row(side="SELL"), changes=cycle(), mutate_bars=flat
    )
    proof = wrapper(signal)["entry_structure"]
    proof["assessment"] = {
        "status": "NOT_APPLICABLE",
        "reason": "PREVIOUS_OPPOSITE_SIDE",
    }
    with pytest.raises(ValueError, match="FIRST_ENTRY_INVALID"):
        check(seal(proof))


def test_full_cycle_controls_require_both_sides_and_enough_bounded_history():
    with pytest.raises(ValueError, match="CYCLE_REQUIRES_BOTH_SIDES"):
        resolve({"reentry.gate_scope": "TRADE_CYCLE"})
    policy = resolve({})["reentry.policy"]
    with pytest.raises(ValueError, match="CYCLE_HISTORY_BUDGET"):
        resolve(
            cycle(
                **{
                    "reentry.policy": {
                        **policy,
                        "timeframe_minutes": 15,
                        "anchor_bars": 200,
                    }
                }
            )
        )


def test_old_immutable_control_digest_verifies_without_policy_upgrade():
    values = {
        k: v
        for k, v in resolve({}).items()
        if k not in {"reentry.gate_scope", "reentry.cycle_policy"}
    }
    saved = {
        "version": 2,
        "values": values,
        "digest": digest(
            canonical({"scope": asdict(SCOPE), "version": 2, "values": values})
        ),
    }
    assert from_snapshot(saved, SCOPE).snapshot() == saved
    damaged = deepcopy(saved)
    damaged["values"]["reentry.gate_scope"] = "CHASE_ONLY"
    with pytest.raises(ValueError, match="PROOF_INVALID"):
        from_snapshot(damaged, SCOPE)


def test_resealed_gate_scope_must_match_saved_controls():
    wrapper, signal, *_ = setup(changes=cycle(), mutate_bars=flat)
    output = wrapper(signal)
    proof = output["entry_structure"]
    proof["gate_scope"] = "CHASE_ONLY"
    with pytest.raises(ValueError, match="CONTROL_MISMATCH"):
        check(seal(proof), expected_controls=output["entry_controls"])


def ready_bars(bars):
    data = flat(bars)
    for b in data:
        if b["t"] == 25 * 60000:
            b.update(h="102", c="101.5", v="20")
        if b["t"] == 26 * 60000:
            b.update(o="101.5", h="102", l="101.4", c="101.6", v="20")
    return data


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "unknown"},
        {"execution_authorized": True},
        {"transition": "OPPOSITE_SIDE_REVERSAL"},
        {"path": "REVERSAL_BREAK_CONFIRM"},
        {"previous_side": "SELL"},
        {"confirmation_bars_observed": 1},
        {"confirmation_bars_observed": True},
        {"confirmation_bars_required": 1},
        {"break_closed_ms": 27 * 60000},
        {"trigger_closed_ms": 27 * 60000 + 1},
        {"trigger_closed_ms": 26 * 60000 + 1},
    ],
)
def test_resealed_ready_still_requires_valid_closed_confirmation(changes):
    wrapper, signal, *_ = setup(changes=cycle(), mutate_bars=ready_bars)
    proof = wrapper(signal)["entry_structure"]
    assert proof["assessment"]["status"] == "READY"
    proof["assessment"].update(changes)
    with pytest.raises(ValueError, match="CYCLE_CONFIRMATION_INVALID"):
        check(seal(proof))


def test_positive_exit_cannot_be_replaced_with_loss_for_direct_breakout():
    wrapper, signal, *_ = setup(changes=cycle(), mutate_bars=ready_bars)
    proof = wrapper(signal)["entry_structure"]
    assert proof["assessment"]["path"] == "NEW_BREAKOUT"
    proof["previous_exit"]["exit_net_pnl"] = "-1"
    with pytest.raises(ValueError, match="PROFIT_BREAKOUT_INVALID"):
        check(seal(proof))


def test_existing_v1_proof_for_supported_exit_remains_valid():
    from v2_core.trade_cycle import opportunity_token

    wrapper, signal, *_ = setup(changes=cycle(), mutate_bars=ready_bars)
    proof = wrapper(signal)["entry_structure"]
    assert proof["assessment"]["status"] == "READY"
    proof["assessment"]["version"] = "trade-cycle-structure-v1"
    proof["assessment"]["opportunity_token"] = opportunity_token(proof["assessment"])
    assert check(seal(proof)) is None


def test_existing_ready_proof_cannot_change_version_by_relabelling_alone():
    wrapper, signal, *_ = setup(changes=cycle(), mutate_bars=ready_bars)
    proof = wrapper(signal)["entry_structure"]
    proof["assessment"]["version"] = "trade-cycle-structure-v1"
    with pytest.raises(ValueError, match="CYCLE_CONFIRMATION_INVALID"):
        check(seal(proof))


@pytest.mark.parametrize("token", [None, "", "a" * 64])
def test_ready_proof_requires_version_and_policy_bound_opportunity_identity(token):
    wrapper, signal, *_ = setup(changes=cycle(), mutate_bars=ready_bars)
    proof = wrapper(signal)["entry_structure"]
    proof["assessment"]["opportunity_token"] = token
    with pytest.raises(ValueError, match="CYCLE_CONFIRMATION_INVALID"):
        check(seal(proof))


def test_external_repair_requires_v2_proof_and_cannot_be_backdated_to_v1():
    def repaired(bars):
        data = flat(bars)
        for b in data:
            if b["t"] == 25 * 60000:
                b.update(h="100", l="98", c="98.5")
            elif b["t"] == 26 * 60000:
                b.update(o="98.5", h="101", l="98.4", c="100.5", v="20")
            elif b["t"] == 27 * 60000:
                b.update(o="100.5", h="101", l="100.3", c="100.6")
        return data

    wrapper, signal, *_ = setup(
        now=28 * 60000 + 1000,
        changes=cycle(),
        mutate_bars=repaired,
        previous=row(
            origin="BINANCE_EXTERNAL_CLOSE", kind="EXTERNAL_REDUCE_ONLY", reason=None
        ),
    )
    proof = wrapper(signal)["entry_structure"]
    assert proof["assessment"]["status"] == "READY"
    assert proof["assessment"]["path"] == "PULLBACK_RESTART"
    assert proof["previous_exit"]["exit_reason"] == "RECONCILED_EXTERNAL_CLOSE"
    assert check(proof) is None
    proof["assessment"]["version"] = "trade-cycle-structure-v1"
    with pytest.raises(ValueError, match="CYCLE_CONFIRMATION_INVALID"):
        check(seal(proof))
