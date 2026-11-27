"""The shipped dormant hypothesis is parseable; this proves no profitability."""

import json
from dataclasses import replace
from pathlib import Path

from services.v2_strategy_candidate_producer import _policies
from services.v2_strategy_runtime_job import SPEC
from tests.v2_core.test_v2_strategy_deployment_execution import binding, material
from v2_core.evidence import canonical
from v2_core.strategy_exit import EXIT_HANDLER, exit_settings

ROOT = Path(__file__).resolve().parents[2]


def test_shipped_configuration_is_explicit_and_compatible_without_activating():
    config = json.loads((ROOT / "deploy/v2-testnet/causal-trend-v1.json").read_text())
    selected = replace(binding(), config_json=canonical(config), mode="OFF")
    builder, sizing, producer = _policies(selected)
    assert builder.min_directional_taker_ratio == "0.55"
    assert sizing.pool_fraction == "0.7"
    # Keep the shipped hypothesis within the conservative rehearsal envelope;
    # deployment does not grant permission to relax an account's tighter limits.
    assert sizing.single_risk_fraction == "0.0025"
    assert sizing.max_margin_consumption == "0.6"
    assert sizing.max_spread_fraction == "0.001"
    assert sizing.depth_multiple == "5"
    assert config["capital"] == sizing.capital_config()
    assert SPEC.exit_handler == EXIT_HANDLER
    _, plan, *_ = material(
        plan_changes={
            "exit_handler": EXIT_HANDLER,
            "exit_version": producer["exit_version"],
            "exit_config_json": canonical(producer["exit_config"]),
        }
    )
    exit_settings(plan)
    assert selected.mode == "OFF"
    assert sizing.boost_enabled is False


def test_explicit_active_overlay_keeps_same_testnet_runtime_limits():
    def values(name):
        return dict(
            line.split("=", 1)
            for line in (ROOT / "deploy/v2-testnet" / name).read_text().splitlines()
            if line and not line.startswith("#")
        )

    paused = values("typed-strategy.env.example")
    active = values("typed-strategy-active.env.example")
    assert active == {
        **paused,
        "V2_CONTROLLER_ENABLE_ENTRIES": "true",
        "V2_ENABLE_ENTRIES": "true",
        "V2_TYPED_RECOVER_PAUSED": "false",
    }
    assert active["V2_ENVIRONMENT"] == "SANDBOX"
