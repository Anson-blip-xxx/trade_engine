"""Exact-bar claims: pure contracts plus explicit isolated-PostgreSQL QA only."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from functools import lru_cache
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_directional_admission import formal
from test_v2_directional_replay import SCOPE
from test_v2_trend_entry import scenario

from services.v2_directional_admission import admission_decision
from services.v2_directional_replay import create_directional_replay_worker
from tests.support.v2_admission import database as database_fixture
from v2_core.evidence import canonical
from v2_core.jev_entry_gate import GateAssessment
from v2_core.producer import market_envelope
from v2_core.runtime import DataRuntime
from v2_core.runtime_policy import PolicyStore
from v2_core.signal_fusion import FusionPolicy, build_fusion
from v2_core.state import BusinessState
from v2_core.strategy import StrategyDecision, StrategyScope
from v2_core.trend_opportunities import (
    NAMESPACE,
    PROFILE,
    SEMANTICS,
    claim_trend_opportunity,
    opportunity_identity,
)

database = database_fixture


@lru_cache(maxsize=4)
def cached_case(side="SHORT", source="s3"):
    signal, context, config = scenario(side, source)
    context["directional"]["expected_move_pct"] = None
    config["mode"] = "TESTNET_ADMISSION_ONLY"
    if source == "tv_bridge":
        config.update(tv_start_ms=1, allowed_symbols=[signal["symbol"]])
    result = admission_decision(signal, context, config)
    assert result.action == "OPEN", result.rationale
    snapshot = {
        "signal_id": str(uuid4()),
        "symbol": signal["symbol"],
        "source": source,
        "observed_at": signal["observed_at"],
        "decided_at": context["assembled_at"],
        "expires_at_ms": context["valid_until_ms"],
        "rationale": result.rationale,
        "result": {
            "action": result.action,
            "side": result.side,
            "quantity": result.quantity,
        },
        "features": {
            "signal": signal["features"],
            "context": context,
            "evaluation": json.loads(result.features_json),
        },
    }
    return (
        StrategyScope(**asdict(SCOPE), producer=config["strategy"].lower()),
        config,
        snapshot,
    )


def case(side="SHORT", source="s3"):
    return deepcopy(cached_case(side, source))


def bind_market(snapshot):
    context = snapshot["features"]["context"]
    market = context["directional"]["market"]
    context["sources"]["s3"] = market_envelope(
        "s3",
        context["environment"],
        context["symbol"],
        market["input_evidence"]["closed_at"],
        market,
    )
    snapshot["features"]["evaluation"]["source_scoring"]["structure"] = deepcopy(
        market["trend_structure"]
    )


def shifted(snapshot, delta):
    """Time-shift a valid fixture; no invented live market evidence."""
    snapshot = deepcopy(snapshot)
    context = snapshot["features"]["context"]
    market = context["directional"]["market"]
    structure = market["trend_structure"]
    for key in ("observed_at", "decided_at", "expires_at_ms"):
        snapshot[key] += delta
    for key in ("assembled_at", "valid_until_ms"):
        context[key] += delta
    market["input_evidence"]["closed_at"] += delta
    structure["closed_at_ms"] += delta
    structure["last_closed_at_ms"] = structure["closed_at_ms"] // 900000 * 900000
    start = structure["closed_at_ms"] - structure["input_bar_count"] * 60000
    complete = structure["closed_at_ms"] // 900000 - (start + 899999) // 900000
    structure["complete_bar_count"] = complete
    structure["indicator_bar_count"] = min(complete, structure["required_bars"])
    structure["excluded_partial_minutes"] = structure["input_bar_count"] - complete * 15
    structure["input_digest"] = "f" * 64
    bind_market(snapshot)
    return snapshot


def test_identity_ignores_source_signal_revision_setup_config_and_tv_metadata():
    scope, config, snapshot = case()
    original = opportunity_identity(scope, config, snapshot)
    tv_scope, tv_config, tv_snapshot = case(source="tv_bridge")
    tv_snapshot["features"]["signal"]["tv_producer"] = {
        "bar_close_ms": 1,
        "version": "untrusted-client-version",
    }
    tv_snapshot["features"]["evaluation"]["source_scoring"]["setup"] = "PULLBACK_RESUME"
    tv_config["policy"]["trend.max_chase_atr"] = "2"
    assert opportunity_identity(tv_scope, tv_config, tv_snapshot) == original
    assert (
        opportunity_identity(
            replace(scope, producer="another-worker"), config, snapshot
        )
        == original
    )
    assert opportunity_identity(scope, config, shifted(snapshot, 60000)) == original
    assert original[1]["profile_version"] == PROFILE


def test_identity_separates_account_symbol_side_and_closed_bar():
    scope, config, snapshot = case()
    original = opportunity_identity(scope, config, snapshot)[0].identity
    separate = []
    another = replace(scope, account_id="another-account")
    account = {
        key: value for key, value in asdict(another).items() if key != "producer"
    }
    cfg, snap = deepcopy(config), deepcopy(snapshot)
    cfg["account_scope"] = account
    snap["features"]["context"]["account_scope"] = account
    separate.append(opportunity_identity(another, cfg, snap)[0].identity)
    snap = deepcopy(snapshot)
    snap["symbol"] = snap["features"]["context"]["symbol"] = "ETHUSDT"
    snap["features"]["context"]["directional"]["market"]["trend_structure"][
        "symbol"
    ] = "ETHUSDT"
    bind_market(snap)
    separate.append(opportunity_identity(scope, config, snap)[0].identity)
    separate.append(opportunity_identity(*case("LONG"))[0].identity)
    separate.append(
        opportunity_identity(scope, config, shifted(snapshot, 900000))[0].identity
    )
    assert len({original, *separate}) == 5


@pytest.mark.parametrize("kind", ["replay", "ignored", "expired", "legacy", "quant"])
def test_non_live_or_non_profile_does_not_touch_database(kind):
    scope, config, snapshot = case()
    if kind == "replay":
        config["mode"] = "REPLAY_ONLY"
    elif kind in {"ignored", "expired"}:
        snapshot["result"]["action"] = kind.upper()
    elif kind == "legacy":
        config["policy"]["entry.trend_structure_enabled"] = False
    else:
        config["source"] = "quant"
    forbidden = SimpleNamespace(execute=lambda *_: pytest.fail("must not access PG"))
    assert (
        claim_trend_opportunity(
            forbidden,
            scope=scope,
            config=config,
            snapshot=snapshot,
            decision_id="not-needed",
        )
        is None
    )


@pytest.mark.parametrize(
    "defect",
    [
        "account",
        "environment",
        "side",
        "profile",
        "client_only",
        "unbound",
        "future_profile",
        "permission",
    ],
)
def test_bad_live_profile_proof_cannot_fall_back_to_rolling_dedup(defect):
    scope, config, snapshot = case()
    evaluation = snapshot["features"]["evaluation"]
    if defect == "account":
        config["account_scope"]["account_id"] = "other"
    elif defect == "environment":
        scope = replace(scope, environment="LIVE")
    elif defect == "side":
        snapshot["result"]["side"] = "BUY"
    elif defect == "profile":
        evaluation.pop("source_scoring")
    elif defect == "client_only":
        snapshot["features"]["signal"]["tv_producer"] = evaluation[
            "source_scoring"
        ].pop("structure")
    elif defect == "unbound":
        snapshot["features"]["context"]["sources"]["s3"]["symbol"] = "ETHUSDT"
    elif defect == "future_profile":
        evaluation["source_scoring"]["profile"] = "causal-trend-entry-v999"
    else:
        evaluation["admission_authorized"] = False
    with pytest.raises(ValueError, match="TREND_OPPORTUNITY"):
        opportunity_identity(scope, config, snapshot)


def claim(database, args, decision=None):
    scope, config, snapshot = args
    with database() as conn:
        return claim_trend_opportunity(
            conn,
            scope=scope,
            config=config,
            snapshot=snapshot,
            decision_id=decision or str(uuid4()),
        )


def claim_count(database):
    with database() as conn:
        return conn.execute(
            "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'=%s",
            (NAMESPACE,),
        ).fetchone()[0]


def test_pg_concurrent_cross_source_claim_and_restart_have_one_winner(database):
    candidates = [case(source="s3" if n % 2 else "tv_bridge") for n in range(8)]
    decisions = [str(uuid4()) for _ in candidates]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda item: claim(database, *item),
                zip(candidates, decisions, strict=True),
            )
        )
    assert [r["status"] for r in results].count("CLAIMED") == 1
    assert [r["status"] for r in results].count("DUPLICATE_OPPORTUNITY") == 7
    winner = next(
        n for n, result in enumerate(results) if result["status"] == "CLAIMED"
    )
    assert (
        claim(database, candidates[winner], decisions[winner])["status"]
        == "ALREADY_CLAIMED"
    )
    assert claim(database, case())["status"] == "DUPLICATE_OPPORTUNITY"
    assert len({r["winning_decision"] for r in results}) == 1
    assert claim_count(database) == 1


def test_pg_new_bar_and_account_have_independent_claims(database):
    scope, config, snapshot = case()
    assert claim(database, (scope, config, snapshot))["status"] == "CLAIMED"
    assert (
        claim(database, (scope, config, shifted(snapshot, 60000)))["status"]
        == "DUPLICATE_OPPORTUNITY"
    )
    assert (
        claim(database, (scope, config, shifted(snapshot, 900000)))["status"]
        == "CLAIMED"
    )
    other_scope = replace(scope, account_id="other")
    account = {
        key: value for key, value in asdict(other_scope).items() if key != "producer"
    }
    config["account_scope"] = account
    snapshot["features"]["context"]["account_scope"] = account
    assert claim(database, (other_scope, config, snapshot))["status"] == "CLAIMED"
    assert claim(database, case("LONG"))["status"] == "CLAIMED"
    assert claim_count(database) == 4


def test_pg_failed_claim_transaction_rolls_back_for_next_candidate(database):
    scope, config, snapshot = case()
    with pytest.raises(RuntimeError, match="transaction failure"), database() as conn:
        assert (
            claim_trend_opportunity(
                conn,
                scope=scope,
                config=config,
                snapshot=snapshot,
                decision_id=str(uuid4()),
            )["status"]
            == "CLAIMED"
        )
        raise RuntimeError("transaction failure")
    assert claim_count(database) == 0
    assert claim(database, case())["status"] == "CLAIMED"


def live_case(
    database, *, source="s3", side="SHORT", entry_filter=None, shift_ms=0, dynamic=False
):
    signal, context, config = deepcopy(scenario(side, source))
    context["directional"]["expected_move_pct"] = None
    if shift_ms:
        updated = shifted(case(side, source)[2], shift_ms)
        context = updated["features"]["context"]
        signal["observed_at"] += shift_ms
        signal["expires_at_ms"] += shift_ms
    if dynamic:
        context["universe_admission"] = {
            "environment": "SANDBOX",
            "symbol": signal["symbol"],
            "eligible": True,
        }
        context["signal_fusion"] = build_fusion(
            signal,
            source=source,
            peers=[],
            policy=FusionPolicy(enabled=True),
            policy_version=1,
            now_ms=context["assembled_at"],
        )
    policy = PolicyStore(database, SCOPE)
    if policy.read().version == 0:
        policy.patch(
            config["policy"], expected_version=0, reason="isolated QA causal profile"
        )
    runtime = DataRuntime(
        database,
        scope=SCOPE,
        clock_ms=lambda: context["assembled_at"],
        submit=lambda _: pytest.fail("QA must never submit"),
        query=lambda _: pytest.fail("QA must never query venue"),
        risk_check=lambda _: pytest.fail("admission must not dispatch"),
    )
    worker = formal(
        runtime,
        config["strategy"],
        source=source,
        max_delay_ms=30000,
        dynamic_universe=dynamic,
        entry_filter=entry_filter,
        **(
            {"tv_start_ms": 1, "allowed_symbols": (signal["symbol"],)}
            if source == "tv_bridge"
            else {}
        ),
    )
    selected = policy.read()
    worker.config_json = canonical(
        {
            **json.loads(worker.config_json),
            "policy": selected.values,
            "policy_version": selected.version,
        }
    )
    signal_id = runtime.data.signals.admit(
        source=source,
        environment="SANDBOX",
        request_key=str(uuid4()),
        snapshot=signal,
    )
    return worker, runtime, signal_id, context


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("source", ["s3", "tv_bridge"])
def test_pg_real_profile_pass_commits_claim_decision_and_prepared_order(
    database, side, source
):
    seen = []

    def assess(original, **kwargs):
        seen.append(original)
        return GateAssessment(
            "PASS",
            "QA_PASS",
            original,
            canonical(
                {
                    "status": "PASS",
                    "execution_authorized": False,
                    "valid_until_ms": kwargs["context"]["valid_until_ms"],
                }
            ),
        )

    worker, runtime, signal, context = live_case(
        database, side=side, source=source, entry_filter=SimpleNamespace(assess=assess)
    )
    result = worker.consume(signal, context=context)
    assert result["status"] == "PREPARED" and len(seen) == 1
    saved = worker.decision(signal)
    evaluation = saved["snapshot"]["features"]["evaluation"]
    assert evaluation["trend_opportunity"]["status"] == "CLAIMED"
    assert evaluation["trend_opportunity"]["semantics"] == SEMANTICS
    assert evaluation["execution_authorized"] is False
    assert claim_count(database) == 1
    worker.decide = lambda *_: pytest.fail("immutable decision must survive restart")
    worker.entry_filter.assess = lambda *_a, **_k: pytest.fail("no second review")
    assert worker.consume(signal, context={})["order_id"] == result["order_id"]
    assert len(runtime.data.trace(result["intent_id"])["orders"]) == 1


@pytest.mark.parametrize("status", ["PENDING", "VETO"])
def test_pg_jev_pending_or_veto_does_not_consume_opportunity(database, status):
    def assess(_original, **_):
        return GateAssessment(
            status,
            "QA_" + status,
            None if status == "PENDING" else StrategyDecision("IGNORED", "QA_VETO"),
            canonical({"status": status, "execution_authorized": False}),
        )

    worker, _, signal, context = live_case(
        database, entry_filter=SimpleNamespace(assess=assess)
    )
    result = worker.consume(signal, context=context)
    assert result["status"] == ("DEFERRED" if status == "PENDING" else "IGNORED")
    assert claim_count(database) == 0


def test_pg_missing_margin_rejects_before_jev_and_claim(database):
    worker, _, signal, context = live_case(
        database,
        entry_filter=SimpleNamespace(
            assess=lambda *_a, **_k: pytest.fail("hard account risk precedes JEV")
        ),
    )
    context["directional"]["sizing"]["available_margin"] = "0"
    assert worker.consume(signal, context=context)["status"] == "IGNORED"
    assert claim_count(database) == 0


def test_pg_replay_does_not_consume_live_opportunity(database):
    admission, runtime, signal, context = live_case(database)
    replay = create_directional_replay_worker(
        runtime, strategy="S8", analysis_mode="hard", max_delay_ms=30000
    )
    replay.config_json = canonical(
        {
            **json.loads(replay.config_json),
            "policy": json.loads(admission.config_json)["policy"],
        }
    )
    assert (
        replay.consume(signal, context=context)["reason"] == "DIRECTIONAL_REPLAY_ONLY"
    )
    assert claim_count(database) == 0
    assert admission.consume(signal, context=context)["status"] == "PREPARED"
    assert claim_count(database) == 1


def test_pg_decision_failure_rolls_back_claim_and_can_retry(database):
    worker, _, signal, context = live_case(database)
    with database() as conn:
        conn.execute(
            "ALTER TABLE v2_strategy_decisions ADD CONSTRAINT qa_failure CHECK (action <> 'OPEN')"
        )
    with pytest.raises(Exception, match="qa_failure"):
        worker.consume(signal, context=context)
    assert claim_count(database) == 0
    assert worker.decision(signal) is None
    with database() as conn:
        conn.execute("ALTER TABLE v2_strategy_decisions DROP CONSTRAINT qa_failure")
    assert worker.consume(signal, context=context)["status"] == "PREPARED"


def test_pg_crash_before_intent_consumes_bar_but_allows_original_retry(database):
    worker, runtime, signal, context = live_case(database)
    accept = runtime.accept_open

    def crashed(*_):
        raise ConnectionError("after committed decision")

    runtime.accept_open = crashed
    with pytest.raises(ConnectionError, match="after committed decision"):
        worker.consume(signal, context=context)
    assert claim_count(database) == 1 and worker.decision(signal)["action"] == "OPEN"
    duplicate, _, second, second_context = live_case(database, source="tv_bridge")
    denied = duplicate.consume(second, context=second_context)
    assert denied["reason"] == "TREND_OPPORTUNITY_ALREADY_CLAIMED"
    proof = duplicate.decision(second)["snapshot"]["features"]["evaluation"]
    assert proof["trend_opportunity"]["winning_signal"] == signal
    assert proof["source_scoring"]["profile"] == PROFILE
    runtime.accept_open = accept
    assert worker.consume(signal, context={})["status"] == "PREPARED"
    assert claim_count(database) == 1


def test_pg_live_environment_cannot_activate_trend_policy(database):
    store = PolicyStore(database, replace(SCOPE, environment="LIVE"))
    with pytest.raises(ValueError, match="TESTNET_TREND_POLICY_ONLY"):
        store.patch(
            {"entry.trend_structure_enabled": True},
            expected_version=0,
            reason="QA rejection",
        )
    assert store.read().version == 0
    assert BusinessState(database).read(store.key) is None


def test_pg_downstream_failure_keeps_claim_but_next_bar_has_no_rolling_cooldown(
    database,
):
    # First opportunity is evaluated in its final minute; the next structure
    # closes only 60 seconds later, inside the old inclusive rolling window.
    worker, _, signal, context = live_case(database, shift_ms=840000, dynamic=True)
    with database() as conn:
        conn.execute(
            "ALTER TABLE v2_domain_outbox ADD CONSTRAINT qa_admission_failure "
            "CHECK (event_type <> 'INTENT_ACCEPTED')"
        )
    result = worker.consume(signal, context=context)
    assert result["status"] == "UNKNOWN"
    assert worker.decision(signal)["action"] == "OPEN"
    assert claim_count(database) == 1
    duplicate, _, second, second_context = live_case(
        database, source="tv_bridge", shift_ms=840000, dynamic=True
    )
    assert duplicate.consume(second, context=second_context)["reason"] == (
        "TREND_OPPORTUNITY_ALREADY_CLAIMED"
    )
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (0,)
        conn.execute(
            "ALTER TABLE v2_domain_outbox DROP CONSTRAINT qa_admission_failure"
        )
    fresh, _, third, third_context = live_case(
        database, source="tv_bridge", shift_ms=900000, dynamic=True
    )
    assert third_context["assembled_at"] - context["assembled_at"] == 60000
    assert fresh.consume(third, context=third_context)["status"] == "PREPARED"
    assert claim_count(database) == 2
