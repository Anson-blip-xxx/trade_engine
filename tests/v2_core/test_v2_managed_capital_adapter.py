"""Shared margin admission for legacy and immutable strategy plans.

No venue requests; database cases use only the disposable QA fixture.
"""

from dataclasses import asdict, replace
from decimal import Decimal, Inexact, localcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_strategy_capital import SCOPE, material

from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountPolicy, AccountRisk
from v2_core.evidence import DecisionEvidence, canonical
from v2_core.intents import AdmissionCode, OpenIntent
from v2_core.managed_portfolio import (
    ManagedPortfolio,
    OpeningCapital,
    PendingMargin,
    _initial_margin,
    _margin,
    _opening_episode,
    _pending_margin,
    _pool_fraction,
    _reserved_margin,
    publish_capital_snapshot,
    reserve_capital,
)
from v2_core.runtime_policy import PolicyStore
from v2_core.service import TradingData
from v2_core.strategy_capital import read_strategy_capital

database = database_fixture


def capital_proof(*, plan=None, margin="91.666666666666666667", risk="15.505"):
    return {
        "reserved_margin": margin,
        "proposed_stop_risk": risk,
        "strategy_plan_digest": (plan or material()[0]).plan_digest,
    }


class Rows:
    def __init__(self, rows=(), one=None):
        self.rows, self.one, self.calls = rows, one, []

    def execute(self, sql, params):
        self.calls.append((sql, params))
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.one


def pending_row(*, new=False, **changes):
    values = {
        "notional": "250",
        "leverage": "2",
        "evidence_ref": "evidence",
        "quantity": "2.5",
        "record": material()[1] if new else None,
        "original_margin": None,
        "created_ms": 50,
        "filled": None,
        "latest_ms": None,
        "open_orders": 1,
        "unresolved": True,
        "scope": asdict(SCOPE),
        **changes,
    }
    original = values["original_margin"]
    values["original_margin"] = (
        capital_proof(margin=original or "91.666666666666666667")
        if new
        else (None if original is None else {"reserved_margin": original})
    )
    return tuple(values.values())


def pending(rows, **changes):
    conn = Rows(rows)
    result = _pending_margin(
        conn, SCOPE, episode=str(uuid4()), buffer=Decimal("1.1"), **changes
    )
    assert len(conn.calls) == 1
    assert "LEFT JOIN v2_decision_evidence" in conn.calls[0][0]
    return result.amount


def test_mixed_pending_batch_uses_frozen_new_leverage_not_legacy_lookalike():
    result = pending([pending_row(), pending_row(new=True, leverage="125")])
    assert result == Decimal("229.166666666666666667")


@pytest.mark.parametrize("new", [False, True])
@pytest.mark.parametrize("unresolved", [False, True])
def test_no_fill_held_reservation_always_retains_full_margin(new, unresolved):
    row = pending_row(new=new, unresolved=unresolved)
    expected = "91.666666666666666667" if new else "137.5"
    assert pending([row], snapshot_started=100) == Decimal(expected)
    assert pending([row]) == Decimal(expected)


@pytest.mark.parametrize("new", [False, True])
def test_full_fill_before_snapshot_is_covered_by_venue(new):
    row = pending_row(new=new, filled="2.5", latest_ms=99, unresolved=False)
    assert pending([row], snapshot_started=100) == 0
    assert pending([row]) == 0


@pytest.mark.parametrize("boundary", ["created_ms", "latest_ms"])
@pytest.mark.parametrize("tick", [100, 101])
def test_snapshot_boundary_retains_full_reservation_conservatively(boundary, tick):
    row = pending_row(
        **{"filled": "2.5", "latest_ms": 99, "unresolved": False, boundary: tick}
    )
    assert pending([row], snapshot_started=100) == Decimal("137.5")


def test_complete_fills_with_unresolved_open_do_not_release_pending_margin():
    row = pending_row(filled="2.5", latest_ms=99)
    assert pending([row], snapshot_started=100) == Decimal("137.5")
    assert pending([row]) == Decimal("137.5")


@pytest.mark.parametrize("filled", ["2.51", "-1"])
@pytest.mark.parametrize("new", [False, True])
def test_overfill_or_negative_fill_is_not_guessed(filled, new):
    row = pending_row(new=new, filled=filled, latest_ms=50, unresolved=False)
    with pytest.raises(ValueError, match="CAPITAL_PENDING_FILL_INVALID"):
        pending([row], snapshot_started=100)


@pytest.mark.parametrize("new", [False, True])
@pytest.mark.parametrize("filled", ["0.1", "2.49"])
def test_partial_open_retains_full_margin_instead_of_poisoning_other_candidates(
    new, filled
):
    row = pending_row(new=new, filled=filled, latest_ms=50, unresolved=False)
    expected = Decimal("91.666666666666666667" if new else "137.5")
    assert pending([row], snapshot_started=100) == expected
    assert pending([row]) == expected
    result = _pending_margin(
        Rows([row]), SCOPE, episode=str(uuid4()), buffer=Decimal("1.1")
    )
    assert result.evidence()["pending_partial_open_reservations"] == 1


@pytest.mark.parametrize("orders", [0, 2, 3])
def test_unknown_new_order_shape_retains_full_reservation(orders):
    row = pending_row(new=True, open_orders=orders, filled="2.5", unresolved=False)
    assert pending([row]) == Decimal("91.666666666666666667")


@pytest.mark.parametrize("new", [False, True])
@pytest.mark.parametrize("filled", [None, "2.5"])
def test_missing_held_evidence_cannot_disappear_from_budget(new, filled):
    row = pending_row(new=new, evidence_ref=None, filled=filled, unresolved=False)
    with pytest.raises(ValueError, match="CAPITAL_PENDING_EVIDENCE_REQUIRED"):
        pending([row], snapshot_started=100)


def test_wrong_scope_and_corrupt_new_marker_never_use_legacy_leverage():
    with pytest.raises(ValueError, match="CAPITAL_PENDING_EVIDENCE_REQUIRED"):
        pending([pending_row(scope={**asdict(SCOPE), "account_id": "other"})])
    for marker in (None, {}, []):
        _, record = material()
        record["snapshot"]["strategy_plan"] = marker
        with pytest.raises(ValueError, match="STRATEGY_PLAN_INTENT_MISMATCH"):
            pending([pending_row(new=True, record=record, leverage="125")])


def test_lower_buffer_cannot_erase_original_reserved_margin():
    row = pending_row(original_margin="180")
    assert pending([row]) == 180
    # Historical non-terminating Decimal serialization is accepted upward.
    historical = "180." + "3" * 97
    assert pending([pending_row(original_margin=historical)]) == Decimal(
        "180.333333333333333334"
    )
    assert _reserved_margin("3." + "3" * 99 + "e-19") == Decimal("0.000000000000000001")


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "1e20", 10, {}])
def test_malformed_original_margin_fails_closed(value):
    with pytest.raises(ValueError, match="CAPITAL_RESERVED_MARGIN_INVALID"):
        _reserved_margin(value)


def test_margin_exact_under_hostile_decimal_context():
    with localcontext() as ctx:
        ctx.prec = 3
        ctx.traps[Inexact] = True
        assert _margin(Decimal(250), 3, Decimal("1.1")) == Decimal(
            "91.666666666666666667"
        )
        assert pending([pending_row(new=True), pending_row()]) == Decimal(
            "229.166666666666666667"
        )


@pytest.mark.parametrize("side,stop", [("BUY", "95"), ("SELL", "105")])
def test_initial_typed_margin_covers_frozen_upper_price_for_both_sides(side, stop):
    _, record = material(plan_changes={"side": side, "stop_price": stop})
    strategy = read_strategy_capital(record, scope=SCOPE)
    with localcontext() as ctx:
        ctx.prec = 3
        ctx.traps[Inexact] = True
        assert _initial_margin(
            Decimal(250), 3, Decimal("1.1"), strategy=strategy
        ) == Decimal("92.583333333333333334")
        # A higher observed reference can never lower an existing margin floor.
        assert _initial_margin(
            Decimal(300), 3, Decimal("1.1"), strategy=strategy
        ) == Decimal(110)


def test_legacy_initial_margin_has_no_new_price_envelope():
    assert _initial_margin(Decimal(250), 3, Decimal("1.1")) == _margin(
        Decimal(250), 3, Decimal("1.1")
    )


def test_initial_typed_margin_refuses_a_different_leverage():
    strategy = read_strategy_capital(material()[1], scope=SCOPE)
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_LEVERAGE_MISMATCH"):
        _initial_margin(Decimal(250), 5, Decimal("1.1"), strategy=strategy)


@pytest.mark.parametrize("value", ["0.700000000000000001", "0.8", "1"])
def test_at_least_thirty_percent_capital_stays_unallocated(value):
    with pytest.raises(ValueError, match="CAPITAL_RESERVE_FLOOR_REQUIRED"):
        _pool_fraction({"capital.pool_fraction": value})


def test_seventy_percent_is_maximum_not_required_utilization():
    assert _pool_fraction({"capital.pool_fraction": "0.70"}) == Decimal("0.70")
    assert _pool_fraction({"capital.pool_fraction": "0.25"}) == Decimal("0.25")


def opening_row(**changes):
    return tuple(
        {
            "episode": str(uuid4()),
            "quantity": "2.5",
            "leg": "OPEN",
            "record": material()[1],
            "evidence_ref": "evidence",
            "has_fills": False,
            "siblings": False,
            "original_margin": capital_proof(margin="100", risk="20"),
            **changes,
        }.values()
    )


def inspect_opening(row, *, proof=None, settings=None):
    return _opening_episode(
        Rows(one=row),
        SCOPE,
        order_id=str(uuid4()),
        proof=proof or {"quantity": "2.5", "leverage": 3},
        settings=settings or {"portfolio_risk.enabled": True},
    )


def test_prepost_full_plan_matches_frozen_leverage_and_retains_original_margin():
    row = opening_row()
    assert inspect_opening(row) == OpeningCapital(row[0], Decimal(100), Decimal(20))


@pytest.mark.parametrize(
    "change",
    [
        {"has_fills": True},
        {"siblings": True},
        {"quantity": "1"},
    ],
)
def test_prepost_new_single_full_open_only(change):
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_SINGLE_OPEN_REQUIRED"):
        inspect_opening(opening_row(**change))


def test_prepost_rejects_partial_proof_quantity():
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_SINGLE_OPEN_REQUIRED"):
        inspect_opening(opening_row(), proof={"quantity": "1", "leverage": 3})


@pytest.mark.parametrize("leverage", [2, 8, "3", True])
def test_prepost_rejects_changed_leverage(leverage):
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_LEVERAGE_MISMATCH"):
        inspect_opening(opening_row(), proof={"quantity": "2.5", "leverage": leverage})


def test_prepost_new_plan_cannot_disable_portfolio_risk():
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_POLICY_REQUIRED"):
        inspect_opening(opening_row(), settings={"portfolio_risk.enabled": False})


@pytest.mark.parametrize(
    "row", [None, opening_row(leg="CLOSE"), opening_row(evidence_ref=None)]
)
def test_prepost_requires_opening_evidence(row):
    with pytest.raises(ValueError, match="CAPITAL_OPENING_EVIDENCE_REQUIRED"):
        inspect_opening(row)


def test_prepost_legacy_does_not_require_synthetic_new_plan():
    row = opening_row(record=None)
    assert inspect_opening(
        row, settings={"portfolio_risk.enabled": False}
    ) == OpeningCapital(row[0], Decimal(100), None)


def configure(database, *, fraction="0.70", portfolio=False):
    AccountRisk(database).configure(
        SCOPE,
        AccountPolicy("USDT", "10000", 20, 0, "qa", 60000),
        expected_version=0,
    )
    PolicyStore(database, SCOPE).patch(
        {
            "capital.enabled": True,
            "capital.pool_fraction": fraction,
            "sizing.pool_fraction": fraction,
            "portfolio_risk.enabled": portfolio,
        },
        expected_version=0,
        reason="isolated margin QA",
    )


@pytest.mark.parametrize(
    "proof",
    [
        None,
        {},
        capital_proof(margin="0"),
        capital_proof(risk="0"),
        {**capital_proof(), "strategy_plan_digest": "wrong-plan"},
        {
            key: value
            for key, value in capital_proof().items()
            if key != "reserved_margin"
        },
    ],
)
def test_new_held_margin_requires_immutable_plan_bound_reservation(proof):
    row = list(pending_row(new=True))
    row[5] = proof
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_RESERVATION_UNVERIFIED"):
        pending([tuple(row)])
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_RESERVATION_UNVERIFIED"):
        inspect_opening(opening_row(original_margin=proof))


def held(database, *, new, filled=None, original_margin=None, status="UNKNOWN"):
    plan, record = material(plan_changes={"opportunity_id": str(uuid4())})
    snapshot = record["snapshot"]
    snapshot.update(source="qa", rationale="isolated margin adapter QA")
    if not new:
        snapshot.pop("strategy_plan")
    # Deliberately deceptive legacy leverage for new plans must be ignored.
    snapshot["features"] = {
        "evaluation": {"market_plan": {"leverage": 125 if new else 2}}
    }
    evidence = DecisionEvidence(
        plan.code_version, canonical(record["config"]), canonical(snapshot)
    )
    intent = OpenIntent(
        str(uuid4()),
        SCOPE.exchange,
        SCOPE.account_id,
        SCOPE.environment,
        SCOPE.product,
        plan.instance_id,
        str(uuid4()),
        plan.symbol,
        plan.side,
        plan.quantity,
        plan.code_version,
        evidence.config_digest,
        evidence.evidence_ref,
    )
    assert TradingData(database).accept(intent, evidence).code is AdmissionCode.ACCEPTED
    order = str(uuid4())
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_episodes(episode_id,slot_key) VALUES(%s,%s)",
            (intent.intent_id, "qa-" + intent.intent_id),
        )
        conn.execute(
            "INSERT INTO v2_orders(order_id,episode_id,leg,client_order_id,request_key,quantity,status) "
            "VALUES(%s,%s,'OPEN',%s,'qa',%s,%s)",
            (
                order,
                intent.intent_id,
                str(uuid4()),
                plan.quantity,
                "UNKNOWN" if filled is not None else status,
            ),
        )
        reference = (
            {
                "capital": capital_proof(
                    plan=plan, margin=original_margin or "91.666666666666666667"
                )
            }
            if new
            else (
                {}
                if original_margin is None
                else {"capital": {"reserved_margin": original_margin}}
            )
        )
        conn.execute(
            "INSERT INTO v2_risk_reservations(episode_id,scope,policy_version,notional,reference) "
            "VALUES(%s,%s,1,250,%s::jsonb)",
            (intent.intent_id, SCOPE.key, canonical(reference)),
        )
    if filled is not None:
        from v2_core.ledger import Ledger

        Ledger(database).record_fill(
            order_id=order,
            exchange_fill_id=str(uuid4()),
            quantity=filled,
            price="100",
            fee="0",
            fee_currency="USDT",
            occurred_at_ms=50,
            evidence={"source": "isolated-qa"},
        )
        if status != "UNKNOWN":
            from v2_core.orders import Orders

            Orders(database).transition(
                order,
                expected_version=1,
                status=status,
                evidence={"source": "isolated-qa"},
            )
    return intent.intent_id, order


def test_pg_mixed_pending_sql_uses_new_and_legacy_frozen_leverage(database):
    configure(database)
    held(database, new=True)
    held(database, new=False)
    with database() as conn:
        assert _pending_margin(
            conn, SCOPE, episode=str(uuid4()), buffer=Decimal("1.1")
        ).amount == Decimal("229.166666666666666667")


@pytest.mark.parametrize("new", [False, True])
def test_pg_partial_open_keeps_full_pending_budget(database, new):
    configure(database)
    held(database, new=new, filled="1")
    with database() as conn:
        result = _pending_margin(
            conn, SCOPE, episode=str(uuid4()), buffer=Decimal("1.1")
        )
        assert result.amount == Decimal("91.666666666666666667" if new else "137.5")
        assert result.partial_open_reservations == 1


def test_pg_complete_fill_only_releases_pending_after_snapshot(database):
    configure(database)
    held(database, new=True, filled="2.5", status="FILLED")
    with database() as conn:
        assert (
            _pending_margin(
                conn, SCOPE, episode=str(uuid4()), buffer=Decimal("1.1")
            ).amount
            == 0
        )
        assert _pending_margin(
            conn,
            SCOPE,
            episode=str(uuid4()),
            buffer=Decimal("1.1"),
            snapshot_started=50,
        ).amount == Decimal("91.666666666666666667")


def test_pg_late_recorded_fill_is_not_covered_by_older_snapshot(database):
    configure(database)
    _, order = held(database, new=True, filled="2.5", status="FILLED")
    with database() as conn:
        recorded_at = conn.execute(
            "SELECT floor(extract(epoch FROM created_at)*1000)::bigint "
            "FROM v2_fills WHERE order_id=%s",
            (order,),
        ).fetchone()[0]
        result = _pending_margin(
            conn,
            SCOPE,
            episode=str(uuid4()),
            buffer=Decimal("1.1"),
            snapshot_started=recorded_at,
        )
        assert result.amount == Decimal("91.666666666666666667")
        assert result.snapshot_overlap_reservations == 1
        assert (
            _pending_margin(
                conn,
                SCOPE,
                episode=str(uuid4()),
                buffer=Decimal("1.1"),
                snapshot_started=recorded_at + 1,
            ).amount
            == 0
        )


def test_pg_margin_budget_boundary_includes_mixed_pending(database):
    configure(database)
    held(database, new=True)
    held(database, new=False)
    publish_capital_snapshot(
        database,
        SCOPE,
        account={
            "totalInitialMargin": "0",
            "availableBalance": "1000",
            "totalWalletBalance": "1000",
            "totalMarginBalance": "1000",
        },
        started=100,
        deadline=200,
        observation_id="qa",
    )
    with database() as conn:
        result = reserve_capital(
            conn,
            SCOPE,
            episode=str(uuid4()),
            notional=Decimal(800),
            leverage=2,
            now=150,
        )
        assert Decimal(result["pending_margin"]) == Decimal("229.166666666666666667")
        assert Decimal(result["reserved_margin"]) == Decimal(440)
    with (
        database() as conn,
        pytest.raises(ValueError, match="ACCOUNT_MARGIN_BUDGET_EXCEEDED"),
    ):
        reserve_capital(
            conn,
            SCOPE,
            episode=str(uuid4()),
            notional=Decimal(900),
            leverage=2,
            now=150,
        )


def test_pg_prepost_rechecks_full_immutable_plan_without_legacy_features(database):
    configure(database, portfolio=True)
    episode, order = held(database, new=True, original_margin="100")
    with database() as conn:
        assert _opening_episode(
            conn,
            SCOPE,
            order_id=order,
            proof={"quantity": "2.5", "leverage": 3},
            settings={"portfolio_risk.enabled": True},
        ) == OpeningCapital(episode, Decimal(100), Decimal("15.505"))


@pytest.mark.parametrize("new", [False, True])
def test_pg_prepost_rejects_other_account_even_without_portfolio_risk(database, new):
    configure(database)
    _, order = held(database, new=new)
    with (
        database() as conn,
        pytest.raises(ValueError, match="CAPITAL_OPENING_EVIDENCE_REQUIRED"),
    ):
        _opening_episode(
            conn,
            replace(SCOPE, account_id="another-account"),
            order_id=order,
            proof={"quantity": "2.5", "leverage": 3},
            settings={"portfolio_risk.enabled": False},
        )


def test_pg_eighty_percent_config_cannot_bypass_runtime_reserve(database):
    configure(database, fraction="0.80")
    with (
        database() as conn,
        pytest.raises(ValueError, match="CAPITAL_RESERVE_FLOOR_REQUIRED"),
    ):
        reserve_capital(
            conn,
            SCOPE,
            episode=str(uuid4()),
            notional=Decimal(1),
            leverage=2,
            now=150,
        )


def test_prepost_cannot_reduce_margin_floor(monkeypatch):
    import v2_core.managed_portfolio as module

    settings = {
        "capital.enabled": True,
        "capital.pool_fraction": "0.7",
        "capital.margin_buffer": "1.1",
    }
    portfolio = object.__new__(ManagedPortfolio)
    portfolio.scope = SCOPE
    portfolio.policy = SimpleNamespace(
        read=lambda: SimpleNamespace(values=settings, version=1, digest="qa")
    )
    monkeypatch.setattr(
        module,
        "_opening_episode",
        lambda *a, **k: OpeningCapital("episode", Decimal(60), None),
    )
    monkeypatch.setattr(
        module, "_pending_margin", lambda *a, **k: PendingMargin(Decimal(0), 0, 0, 0)
    )
    monkeypatch.setattr(module, "current_health", lambda *a, **k: {"base": "100"})
    monkeypatch.setattr(module, "guard_risk", lambda *a, **k: {})
    proof = {
        "started_at_ms": 100,
        "quantity": "1",
        "leverage": 2,
        "reference": {"mark_price": "100"},
        "inventory": {
            "responses": {
                "account": {
                    "totalInitialMargin": "0",
                    "availableBalance": "100",
                    "totalWalletBalance": "100",
                    "totalMarginBalance": "100",
                }
            }
        },
    }
    result = portfolio.budget(Rows(), order_id="order", proof=proof)
    assert result["proposed_margin"] == "60"
    assert Decimal(result["remaining_after"]) == 10


@pytest.mark.parametrize("change", ["buffer", "mark_price", "cost"])
def test_new_prepost_cannot_expand_immutable_reservation(monkeypatch, change):
    import v2_core.managed_portfolio as module

    settings = {
        "capital.enabled": True,
        "capital.pool_fraction": "0.7",
        "capital.margin_buffer": "1.3" if change == "buffer" else "1.1",
    }
    portfolio = object.__new__(ManagedPortfolio)
    portfolio.scope = SCOPE
    portfolio.policy = SimpleNamespace(
        read=lambda: SimpleNamespace(values=settings, version=1, digest="qa")
    )
    monkeypatch.setattr(
        module,
        "_opening_episode",
        lambda *a, **k: OpeningCapital("episode", Decimal(60), Decimal(5)),
    )
    observed = []

    def pending_snapshot(*args, **kwargs):
        observed.append(kwargs["snapshot_started"])
        return PendingMargin(Decimal(0), 0, 0, 0)

    monkeypatch.setattr(module, "_pending_margin", pending_snapshot)
    monkeypatch.setattr(module, "current_health", lambda *a, **k: {"base": "100"})
    monkeypatch.setattr(
        module,
        "guard_risk",
        lambda *a, **k: {"proposed_stop_risk": "6" if change == "cost" else "5"},
    )
    proof = {
        "started_at_ms": 100,
        "quantity": "1",
        "leverage": 2,
        "reference": {"mark_price": "121" if change == "mark_price" else "100"},
        "inventory": {
            "responses": {
                "account": {
                    "totalInitialMargin": "0",
                    "availableBalance": "100",
                    "totalWalletBalance": "100",
                    "totalMarginBalance": "100",
                }
            }
        },
    }
    with pytest.raises(
        ValueError, match="STRATEGY_CAPITAL_RESERVATION_EXPANSION_REQUIRED"
    ):
        portfolio.budget(Rows(), order_id="order", proof=proof)
    assert observed == [100]


@pytest.mark.parametrize("started", [None, True, -1, "100"])
def test_prepost_requires_inventory_time_for_pending_margin(monkeypatch, started):
    import v2_core.managed_portfolio as module

    portfolio = object.__new__(ManagedPortfolio)
    portfolio.scope = SCOPE
    portfolio.policy = SimpleNamespace(
        read=lambda: SimpleNamespace(
            values={"capital.enabled": True, "capital.pool_fraction": "0.7"}
        )
    )
    monkeypatch.setattr(
        module,
        "_opening_episode",
        lambda *a, **k: OpeningCapital("episode", Decimal(60), None),
    )
    with pytest.raises(ValueError, match="CAPITAL_SNAPSHOT_TIME_REQUIRED"):
        portfolio.budget(Rows(), order_id="order", proof={"started_at_ms": started})
