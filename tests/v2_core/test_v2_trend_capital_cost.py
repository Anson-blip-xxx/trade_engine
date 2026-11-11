"""Frozen trend costs survive admission, reservation re-reads and policy changes."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal

import pytest

from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope
from v2_core.capital_model import guard_risk, portfolio_budget, rehearsal_profile
from v2_core.evidence import DecisionEvidence
from v2_core.managed_portfolio import publish_capital_snapshot
from v2_core.runtime_policy import PolicyStore, resolve

database = database_fixture
SCOPE = AccountScope("BINANCE", "test-account", "SANDBOX", "FUTURES")
PROFILE = "causal-trend-entry-v1"
CAPITAL = {"base": "1000", "factor": "1"}


def settings(**changes):
    return resolve(
        {
            "capital.enabled": True,
            "sizing.pool_fraction": "0.70",
            "portfolio_risk.enabled": True,
            "entry.trend_structure_enabled": True,
            "sizing.cost_buffer_fraction": "0",
            **changes,
        }
    )


def scoring(**changes):
    return {
        "version": PROFILE,
        "profile": PROFILE,
        "cost_buffer_fraction": "0.002",
        **changes,
    }


def plan(mode="TREND_BREAKOUT"):
    return {
        "stop_fraction": "0.005",
        "entry_mode": mode,
        "side": "LONG",
        "leverage": 2,
        "margin_mode": "ISOLATED",
    }


def held(proof, *, mode="TREND_BREAKOUT", original="5", notional="1000"):
    return (notional, "0.005", original, "10", "99.5", "LONG", proof, mode)


class ReadOnlyRows:
    """Only fake SELECT results; the PG test below verifies the real SQL shape."""

    def __init__(self, proof, *, rows=(), mode="TREND_BREAKOUT", envelope=None):
        self.rows = rows
        self.candidate = (
            plan(mode),
            None,
            "BTCUSDT",
            envelope,
            {"reason": "SIZED", "quantity": "10", "stop_price": "99.5"},
            "10",
            proof,
        )

    def execute(self, sql, _args):
        assert sql.lstrip().startswith("SELECT")
        return self

    def fetchone(self):
        return self.candidate

    def fetchall(self):
        return self.rows


def proposed(rows, configured=None):
    return guard_risk(
        rows,
        SCOPE,
        episode="00000000-0000-0000-0000-000000000001",
        notional=Decimal(1000),
        leverage=2,
        settings=configured or settings(),
        capital=CAPITAL,
    )


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("current,expected", [("0", "7"), (".001", "7"), (".004", "9")])
def test_frozen_cost_is_used_for_proposed_and_held_after_policy_changes(
    enabled, current, expected
):
    configured = settings(
        **{
            "entry.trend_structure_enabled": enabled,
            "trend.roundtrip_cost_fraction": ".0001",
            "sizing.cost_buffer_fraction": current,
        }
    )
    before = deepcopy(configured)
    assert Decimal(
        proposed(ReadOnlyRows(scoring()), configured)["proposed_stop_risk"]
    ) == Decimal(expected)
    result = portfolio_budget(
        ReadOnlyRows(scoring(), rows=[held(scoring())]),
        SCOPE,
        settings=configured,
        capital=CAPITAL,
    )
    assert Decimal(result["held_stop_risk"]) == Decimal(expected)
    assert Decimal(result["remaining_stop_risk"]) == 30 - Decimal(expected)
    assert configured == before


@pytest.mark.parametrize(
    "proof",
    [
        None,
        {},
        [],
        {"profile": PROFILE, "cost_buffer_fraction": ".002"},
        {"version": PROFILE, "cost_buffer_fraction": ".002"},
        scoring(version="other"),
        scoring(profile="other"),
        {"profile": PROFILE, "version": PROFILE},
        *[
            scoring(cost_buffer_fraction=value)
            for value in (
                None,
                0.002,
                True,
                "0",
                "-.002",
                "NaN",
                "Infinity",
                ".1001",
                ".00001",
                "1e-19",
                "9" * 101,
            )
        ],
    ],
)
@pytest.mark.parametrize("stage", ["proposed", "held"])
def test_structural_cost_evidence_fails_closed(proof, stage):
    with pytest.raises(ValueError, match="CAPITAL_TREND_COST_UNVERIFIED"):
        if stage == "proposed":
            proposed(ReadOnlyRows(proof))
        else:
            portfolio_budget(
                ReadOnlyRows(proof, rows=[held(proof)]),
                SCOPE,
                settings=settings(),
                capital=CAPITAL,
            )


@pytest.mark.parametrize("marker", ["profile", "version"])
def test_profile_marker_prevents_missing_mode_from_downgrading_validation(marker):
    with pytest.raises(ValueError, match="CAPITAL_TREND_COST_UNVERIFIED"):
        proposed(ReadOnlyRows({marker: PROFILE}, mode=None))


@pytest.mark.parametrize(
    "mode,proof",
    [
        (None, None),
        ("TREND", {"profile": "directional-default-v1"}),
        ("QUANT", {"profile": "quant", "cost_buffer_fraction": "bad"}),
    ],
)
def test_legacy_and_quant_keep_runtime_cost_semantics(mode, proof):
    configured = settings(**{"sizing.cost_buffer_fraction": ".001"})
    assert (
        Decimal(
            proposed(ReadOnlyRows(proof, mode=mode), configured)["proposed_stop_risk"]
        )
        == 6
    )
    result = portfolio_budget(
        ReadOnlyRows(proof, rows=[held(proof, mode=mode)]),
        SCOPE,
        settings=configured,
        capital=CAPITAL,
    )
    assert Decimal(result["held_stop_risk"]) == 6


def test_cost_is_per_episode_and_original_recorded_risk_remains_a_floor():
    rows = [
        held(scoring(), original="8"),
        held(None, mode=None, original=None),
        held({"profile": "quant"}, mode="QUANT", original=None),
    ]
    result = portfolio_budget(
        ReadOnlyRows(scoring(), rows=rows), SCOPE, settings=settings(), capital=CAPITAL
    )
    assert Decimal(result["held_stop_risk"]) == 18


def test_price_envelope_and_fixed_stop_use_the_same_retained_cost():
    envelope = {
        "version": "remaining-risk-sizing-v1",
        "reference_price": "100",
        "mark_price": "100",
        "price_drift_fraction": ".001",
    }
    assert Decimal(
        proposed(ReadOnlyRows(scoring(), envelope=envelope))["proposed_stop_risk"]
    ) == Decimal("8.002")
    result = portfolio_budget(
        ReadOnlyRows(scoring(), rows=[held(scoring(), notional="1001")]),
        SCOPE,
        settings=settings(),
        capital=CAPITAL,
    )
    assert Decimal(result["held_stop_risk"]) == Decimal("8.002")


def test_cost_floor_blocks_single_and_portfolio_oversubscription():
    with pytest.raises(ValueError, match="CAPITAL_SINGLE_TRADE_RISK_EXCEEDED"):
        proposed(ReadOnlyRows(scoring()), settings(**{"sizing.risk_fraction": ".006"}))
    with pytest.raises(ValueError, match="CAPITAL_PORTFOLIO_RISK_EXCEEDED"):
        proposed(
            ReadOnlyRows(scoring()), settings(**{"portfolio_risk.max_fraction": ".006"})
        )
    # Three existing 7-unit obligations leave 9, insufficient for another 10.5.
    with pytest.raises(ValueError, match="CAPITAL_PORTFOLIO_RISK_EXCEEDED"):
        proposed(
            ReadOnlyRows(
                scoring(cost_buffer_fraction=".0055"), rows=[held(scoring())] * 3
            ),
            settings(**{"sizing.risk_fraction": ".02"}),
        )


def test_pg_concurrent_reservations_and_disabled_profile_retain_frozen_cost(
    database, monkeypatch
):
    """Uses only the explicitly isolated QA database and mocked venue callbacks."""
    import test_v2_account_risk as helpers
    from test_v2_capital_model import account

    original = helpers.evidence()
    snapshot = json.loads(original.snapshot_json)
    snapshot["features"] = {
        "evaluation": {
            "market_plan": plan(),
            "source_scoring": scoring(),
            "sizing": {"reason": "SIZED", "quantity": "1", "stop_price": "99.5"},
        }
    }
    proof = DecisionEvidence(
        original.strategy_version, original.config_json, json.dumps(snapshot)
    )
    monkeypatch.setattr(helpers, "evidence", lambda: proof)
    store = PolicyStore(database, SCOPE)
    store.patch(
        {
            **rehearsal_profile("10000"),
            "entry.trend_structure_enabled": True,
            "sizing.cost_buffer_fraction": "0",
            "portfolio_risk.max_fraction": ".003",
        },
        expected_version=0,
        reason="isolated QA trend cost floor",
    )
    risk = helpers.configured(database, max_notional=None, max_positions=None)
    now = helpers.now()
    publish_capital_snapshot(
        database,
        SCOPE,
        account=account(),
        started=now,
        deadline=now + 60000,
        observation_id="trend-cost-concurrent-risk",
    )
    orders = [
        helpers.prepare(database, symbol=f"ASSET{i}USDT", quantity="1")[2]
        for i in range(7)
    ]
    with ThreadPoolExecutor(max_workers=7) as pool:
        results = list(pool.map(helpers.runner(database).dispatch, orders))
    # At 100 notional, .005 stop + .002 frozen cost consumes .7 of the 3-unit cap.
    assert results.count("ACKNOWLEDGED") == 4
    assert results.count("DENIED") == 3
    assert risk.usage(SCOPE)["positions"] == 4
    with database() as conn:
        recorded = conn.execute(
            "SELECT reference->'capital'->>'proposed_stop_risk' FROM v2_risk_reservations WHERE scope=%s AND status='HELD'",
            (SCOPE.key,),
        ).fetchall()
    assert len(recorded) == 4 and all(
        Decimal(row[0]) == Decimal(".7") for row in recorded
    )
    store.patch(
        {
            "entry.trend_structure_enabled": False,
            "trend.roundtrip_cost_fraction": ".0001",
        },
        expected_version=1,
        reason="isolated QA disable must retain frozen costs",
    )
    with database() as conn:
        retained = portfolio_budget(
            conn, SCOPE, settings=store.read().values, capital=CAPITAL
        )
    assert Decimal(retained["held_stop_risk"]) == Decimal("2.8")
    assert Decimal(retained["remaining_stop_risk"]) == Decimal(".2")
    later = helpers.prepare(database, symbol="LATERUSDT", quantity="1")[2]
    assert helpers.runner(database).dispatch(later) == "DENIED"
    store.patch(
        {"sizing.cost_buffer_fraction": ".003"},
        expected_version=2,
        reason="isolated QA increased current costs",
    )
    with database() as conn:
        tightened = portfolio_budget(
            conn, SCOPE, settings=store.read().values, capital=CAPITAL
        )
    assert Decimal(tightened["held_stop_risk"]) == Decimal("3.2")
    assert Decimal(tightened["remaining_stop_risk"]) == 0
