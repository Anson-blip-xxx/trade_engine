"""Verified old/new accounting is shared, scoped and never a deposit profit.

New-plan positives use the real cash and settlement services with fake venue
responses. Explicit historical/forged rows below are confined to disposable PG
and exercise the reader's provenance boundary, not a settlement-writing API.
"""

from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
from uuid import uuid4

import pytest
from test_v2_strategy_settlement import SCOPE, closed_plan

from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import evidence, intent
from v2_core.capital_model import (
    anchor,
    current_health,
    rehearsal_profile,
    verified_settled_outcomes,
)
from v2_core.evidence import canonical
from v2_core.intents import AdmissionCode
from v2_core.runtime_policy import resolve
from v2_core.service import TradingData
from v2_core.state import BusinessState, StateKey

database = database_fixture


def completed(database):
    case = closed_plan(database)
    result = case.stage.run_once()
    assert result["settlements"][case.episode]["status"] == "SETTLED", result
    return case


def append_historical_revision(
    database, episode, payload, *, revision, currency="USDT"
):
    # Explicit malformed/history fixture injection; production settlement is
    # immutable and must use the journal service, not this test-only helper.
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_settlements(episode_id,revision,currency,evidence) "
            "VALUES(%s,%s,%s,%s::jsonb)",
            (episode, revision, currency, canonical(payload)),
        )
        conn.execute(
            "UPDATE v2_episodes SET accounting_revision=%s,status='SETTLED' WHERE episode_id=%s",
            (revision, episode),
        )


def legacy_settlement(
    database,
    *,
    scope=SCOPE,
    net="2",
    source="directional-settlement-v1",
    currency="USDT",
    extra=None,
):
    selected = replace(intent(), **asdict(scope))
    assert (
        TradingData(database).accept(selected, evidence()).code
        is AdmissionCode.ACCEPTED
    )
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_episodes(episode_id,slot_key) VALUES(%s,%s)",
            (selected.intent_id, "qa-capital-history:" + str(uuid4())),
        )
    append_historical_revision(
        database,
        selected.intent_id,
        {"source": source, "net_pnl": net, **(extra or {})},
        revision=1,
        currency=currency,
    )
    return selected.intent_id


def account(wallet):
    return {
        "totalWalletBalance": wallet,
        "totalMarginBalance": wallet,
        "availableBalance": wallet,
        "totalInitialMargin": "0",
        "verified_net_pnl": "999999",  # Caller input is not journal proof.
    }


def test_current_revision_old_and_real_new_settlements_count_exactly_once(database):
    case = completed(database)
    old = legacy_settlement(database, net="-1")
    append_historical_revision(
        database,
        old,
        {"source": "directional-settlement-v1", "net_pnl": "2"},
        revision=2,
    )
    outcomes = verified_settled_outcomes(database, SCOPE, 0)
    assert len(outcomes) == 2
    assert sorted(map(Decimal, outcomes)) == [case.net, Decimal(2)]
    assert sum(map(Decimal, outcomes)) == case.net + 2
    assert verified_settled_outcomes(database, SCOPE, 10**15) == ()


@pytest.mark.parametrize(
    "scope_change",
    [
        {"account_id": "qa-other-account"},
        {"environment": "LIVE"},
    ],
)
def test_other_scope_does_not_contribute_to_capital(database, scope_change):
    case = completed(database)
    legacy_settlement(database, scope=replace(SCOPE, **scope_change), net="500")
    assert tuple(map(Decimal, verified_settled_outcomes(database, SCOPE, 0))) == (
        case.net,
    )


@pytest.mark.parametrize("change", [{"product": "SPOT"}, {"exchange": "OTHER"}])
def test_unsupported_capital_scope_cannot_be_constructed(change):
    with pytest.raises(ValueError, match="explicit linear futures account scope"):
        replace(SCOPE, **change)


@pytest.mark.parametrize("source", ["unknown", "manual", "strategy-settlement-v2"])
def test_unknown_sources_never_become_reinvestable_profit(database, source):
    case = completed(database)
    legacy_settlement(database, net="500", source=source)
    assert tuple(map(Decimal, verified_settled_outcomes(database, SCOPE, 0))) == (
        case.net,
    )


def test_latest_unknown_revision_cannot_fall_back_to_old_profit(database):
    old = legacy_settlement(database, net="50")
    append_historical_revision(
        database, old, {"source": "unknown", "net_pnl": "-10"}, revision=2
    )
    assert verified_settled_outcomes(database, SCOPE, 0) == ()


def test_other_settlement_currency_is_not_assumed_usdt(database):
    legacy_settlement(database, net="500", currency="BTC")
    assert verified_settled_outcomes(database, SCOPE, 0) == ()


@pytest.mark.parametrize(
    "defect",
    ["plan_digest", "account_scope", "legacy_source", "cash_basis", "cash_policy"],
)
def test_corrupt_recognized_new_source_fails_closed(database, defect):
    case = completed(database)
    with database() as conn:
        revision, original = conn.execute(
            "SELECT revision,evidence FROM v2_settlements WHERE episode_id=%s",
            (case.episode,),
        ).fetchone()
    forged = deepcopy(original)
    if defect == "plan_digest":
        forged["strategy_plan"]["plan_digest"] = "a" * 64
    elif defect == "account_scope":
        forged["account_scope"]["account_id"] = "qa-other"
    elif defect == "legacy_source":
        forged["source"] = "directional-settlement-v1"
    elif defect == "cash_basis":
        del forged["cash_basis"]
    else:
        forged["cash_policy_snapshot"]["values"]["settlement.cash_basis_version"] = 1
    append_historical_revision(database, case.episode, forged, revision=revision + 1)
    with pytest.raises(ValueError, match="STRATEGY_SETTLEMENT_PROVENANCE_MISMATCH"):
        verified_settled_outcomes(database, SCOPE, 0)


@pytest.mark.parametrize(
    "source,extra",
    [
        ("strategy-settlement-v1", {}),
        ("directional-settlement-v1", {"strategy_plan": {"plan_digest": "a" * 64}}),
    ],
)
def test_legacy_evidence_cannot_masquerade_as_new_settlement(database, source, extra):
    legacy_settlement(database, source=source, extra=extra)
    with pytest.raises(ValueError, match="STRATEGY_SETTLEMENT_PROVENANCE_MISMATCH"):
        verified_settled_outcomes(database, SCOPE, 0)


def test_later_cash_revision_makes_previous_settlement_unverified(database):
    case = completed(database)
    case.data.ledger.adjustment(
        episode_id=case.episode,
        source_id="qa-late-cash",
        amount_text="-1",
        currency="USDT",
        kind="CORRECTION",
        occurred_at_ms=case.tick + 70001,
        evidence={"source": "explicit-late-cash-QA"},
    )
    assert verified_settled_outcomes(database, SCOPE, 0) == ()


def test_current_health_compounds_only_journal_profit_visible_in_wallet(database):
    case = completed(database)
    settings = resolve(rehearsal_profile("1000"))
    visible_wallet = str(Decimal(1000) + case.net)
    result = current_health(database, SCOPE, account(visible_wallet), settings)
    assert Decimal(result["base"]) == Decimal(1000) + case.net / 2
    # A deposit does not increase the proven profit, and a missing wallet gain
    # does not become spendable merely because the journal has settled it.
    deposit = current_health(database, SCOPE, account("2000"), settings)
    absent = current_health(database, SCOPE, account("1000"), settings)
    assert Decimal(deposit["base"]) == Decimal(result["base"])
    assert Decimal(absent["base"]) == 1000


@pytest.mark.parametrize(
    "probe_started_at_ms,expected_mode", [(0, "ACTIVE"), (10**15, "PROBE")]
)
def test_current_health_probe_uses_new_verified_settlement_and_cutoff(
    database, probe_started_at_ms, expected_mode
):
    case = completed(database)
    settings = resolve(
        {
            **rehearsal_profile("1000"),
            "recovery.enabled": True,
            "recovery.min_settlements": 1,
        }
    )
    key = StateKey(**asdict(SCOPE), namespace="capital-snapshot-v1", key="latest")
    store = BusinessState(database)
    saved = store.read(key)
    assert saved is not None
    store.change(
        key,
        expected_version=saved.version,
        request_key="qa-probe",
        payload={
            "capital_health": {
                "anchor": anchor(settings),
                "base": "1000",
                "peak": "1000",
                "factor": "0.2",
                "drawdown": "0",
                "recovery": {
                    "mode": "PROBE",
                    "attempts": 1,
                    "probe_started_at_ms": probe_started_at_ms,
                    "probe_base": "1000",
                },
            }
        },
        reason="QA_PROBE_STATE",
    )
    result = current_health(
        database,
        SCOPE,
        account(str(Decimal(1000) + case.net)),
        settings,
        now_ms=case.tick + 70001,
    )
    assert result["recovery"]["mode"] == expected_mode
    assert result["recovery"]["attempts"] == 1
    assert Decimal(result["base"]) == Decimal(1000) + case.net / 2
    assert Decimal(result["factor"]) == (
        Decimal(1) if expected_mode == "ACTIVE" else Decimal(".2")
    )
