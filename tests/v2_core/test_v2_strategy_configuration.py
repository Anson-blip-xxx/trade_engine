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
