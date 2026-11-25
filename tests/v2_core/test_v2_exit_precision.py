"""Derived exit inputs must fit the ledger domain without changing raw fills."""

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal, localcontext
from types import SimpleNamespace

import pytest

from services.v2_directional_exit import DirectionalExitStage
from v2_core.account_risk import AccountScope
from v2_core.directional_exit import ExitFacts, evaluate_directional_exit
from v2_core.ledger import amount

SCOPE = AccountScope("BINANCE", "precision-qa", "SANDBOX", "FUTURES")
EPISODE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def trace_case(fills, closed="0", planned="4.840396"):
    trace = {
        "scope": {**asdict(SCOPE), "producer": "s8"},
        "configuration": {
            "mode": "TESTNET_ADMISSION_ONLY",
            "account_scope": asdict(SCOPE),
        },
        "decision": {
            "features": {
                "evaluation": {
                    "admission_authorized": True,
                    "execution_market_gate": "MARKET_GATES_PASSED",
                    "market_plan": {"system_tag": "S8"},
                    "sizing": {"reason": "SIZED", "planned_loss": planned},
                }
            }
        },
        "orders": [{"order_id": "open", "leg": "OPEN", "status": "FILLED"}],
        "fills": [
            {"order_id": "open", "quantity": q, "price": p, "occurred_at_ms": 1}
            for q, p in fills
        ],
    }
    if Decimal(closed):
        trace["orders"].append(
            {
                "order_id": "close",
                "leg": "CLOSE",
                "status": "FILLED",
                "request_key": "exit:partial-take-profit",
            }
        )
        trace["fills"].append(
            {
                "order_id": "close",
                "quantity": closed,
                "price": "0.006",
                "occurred_at_ms": 2,
            }
        )
    service = object.__new__(DirectionalExitStage)
    service.scope = SCOPE
    service.data = SimpleNamespace(trace=lambda _: deepcopy(trace))

    class PlanConnection:
        def execute(self, statement, params):
            # Keep the real immutable-plan reader in the path. This precision
            # fixture is a legacy episode, whose evidence has no typed marker.
            assert "JOIN v2_decision_evidence e USING(evidence_ref)" in statement
            assert params == (EPISODE,)
            return SimpleNamespace(
                fetchone=lambda: (
                    *asdict(SCOPE).values(),
                    "s8",
                    "directional-admission-v2-1",
                    "legacy-config",
                    {},
                    "directional-admission-v2-1",
                    "legacy-config",
                    deepcopy(trace["configuration"]),
                    deepcopy(trace["decision"]),
                )
            )

    service.connect = lambda: nullcontext(PlanConnection())
    return service, trace


@pytest.mark.parametrize("precision", [6, 28, 100])
@pytest.mark.parametrize(
    "fills,closed,planned",
    [
        # Real INX multi-price quantities: weighted entry has 102 decimal places.
        (
            [("7825", "0.006322"), ("799", "0.006318"), ("942", "0.006318")],
            "0",
            "4.840396",
        ),
        (
            [("7825", "0.006322"), ("799", "0.006318"), ("942", "0.006318")],
            "2869",
            "4.840396",
        ),
        ([("1", "0.006321"), ("2", "0.006322")], "1", "1"),
        # Positive remaining risk below one ledger quantum must not become zero.
        ([("1", "0.006321"), ("2", "0.006322")], "2", "0.000000000000000001"),
    ],
)
def test_multifill_and_partial_exit_inputs_are_bounded(
    precision, fills, closed, planned
):
    service, original = trace_case(fills, closed, planned)
    before = deepcopy(original)
    with localcontext() as ctx:
        ctx.prec = precision
        _, _, _, _, entry, remaining, risk, _, partial, pending = service._trace(
            EPISODE
        )
        amount(str(entry), positive=True)
        amount(str(risk), positive=True)
        decision = evaluate_directional_exit(
            ExitFacts(
                system_tag="S8",
                side="SELL",
                entry_price=str(entry),
                mark_price="0.00632",
                stop_price="0.006838",
                remaining_quantity=str(remaining),
                planned_loss=str(risk),
                held_ms=1000,
                funding_rate="0",
                ema9_1h="0.00632",
                ema20_1h="0.00632",
                momentum_closes_15m=("0.00632",) * 4,
                peak_return_pct="0",
                partial_done=partial,
                exit_fee_rate="0.0004",
            )
        )
    assert decision.action == "WAIT"
    assert not pending
    assert original == before
    with localcontext() as ctx:
        ctx.prec = 100
        opened = sum(Decimal(q) for q, _ in fills)
        exact_entry = sum(Decimal(q) * Decimal(p) for q, p in fills) / opened
        exact_risk = Decimal(planned) * (opened - Decimal(closed)) / opened
        assert abs(entry - exact_entry) <= Decimal("5e-19")
        assert Decimal(0) <= risk - exact_risk < Decimal("1e-18")
        assert remaining == opened - Decimal(closed)


@pytest.mark.parametrize("bad_price", ["NaN", "Infinity", "1e20", "1e-19", "0", "-1"])
def test_raw_fill_validation_is_not_relaxed(bad_price):
    service, _ = trace_case([("1", bad_price)])
    with pytest.raises(ValueError):
        service._trace(EPISODE)
