"""S6/S8 Testnet strategy admission: real rules -> durable PREPARED, never send.

Admission is not a trading permit. Fresh account/margin, protection readiness,
venue leverage/margin setup and reconciliation must still gate dispatch.
"""

import json
from dataclasses import asdict

from services.v2_directional_replay import replay_decision
from v2_core.directional import supports_event
from v2_core.evidence import canonical
from v2_core.runtime import DataRuntime
from v2_core.scheduling import StrategyScheduler
from v2_core.strategy import StrategyDecision, StrategyScope, StrategyWorker


def admission_decision(signal, context, config):
    if (
        config.get("mode") != "TESTNET_ADMISSION_ONLY"
        or config.get("account_scope", {}).get("environment") != "SANDBOX"
    ):
        raise ValueError("explicit testnet admission configuration required")
    # One evaluator for replay and admission prevents drifting strategy rules.
    evaluated = replay_decision(signal, context, {**config, "mode": "REPLAY_ONLY"})
    if evaluated.rationale != "DIRECTIONAL_REPLAY_ONLY":
        return evaluated
    features = json.loads(evaluated.features_json)
    features["admission_authorized"] = True
    # The feature remains false: PREPARED is not permission to call the venue.
    features["execution_authorized"] = False
    return StrategyDecision(
        "OPEN",
        "DIRECTIONAL_MARKET_GATES_PASSED",
        side={"LONG": "BUY", "SHORT": "SELL"}[features["market_plan"]["side"]],
        quantity=features["sizing"]["quantity"],
        features_json=canonical(features),
    )


def create_directional_admission_worker(
    runtime, *, strategy, analysis_mode, max_delay_ms, enable_admission=False
):
    # Fail at construction instead of consuming formal signals while disabled.
    if type(enable_admission) is not bool or not enable_admission:
        raise ValueError("DIRECTIONAL_ADMISSION_DISABLED")
    if (
        not isinstance(runtime, DataRuntime)
        or runtime.scope is None
        or runtime.scope.environment != "SANDBOX"
        or runtime.execution.scope != runtime.scope
        or not callable(runtime.execution.risk_reference)
    ):
        raise ValueError("account-bound testnet runtime with risk reference required")
    if strategy not in {"S6", "S8"} or analysis_mode not in {"hard", "soft"}:
        raise ValueError("explicit directional strategy and analysis mode required")
    account = asdict(runtime.scope)
    return StrategyWorker(
        runtime,
        StrategyScope(**account, producer=strategy.lower()),
        source="s3",
        strategy_version="directional-admission-v2-1",
        config={
            "strategy": strategy,
            "analysis_mode": analysis_mode,
            "mode": "TESTNET_ADMISSION_ONLY",
            "account_scope": account,
        },
        decide=admission_decision,
        max_delay_ms=max_delay_ms,
    )


def create_directional_admission_scheduler(runtime, *, context_provider, **settings):
    """Bounded PG scheduler; construction does not start a loop or send orders."""
    return StrategyScheduler(
        create_directional_admission_worker(runtime, **settings),
        context_provider=context_provider,
        context_required=lambda snapshot: supports_event(
            settings["strategy"], snapshot["signal"]
        ),
    )
