"""Executable DOM contract tests; real browser acceptance is reported separately."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", [
    "races_and_retry", "percentages", "tabs_and_trades", "load_failures",
    "expiry_and_restore", "account_mismatch", "readonly_legacy", "readonly_review", "risk_review", "reentry_shadow", "shadow_evaluation",
])
def test_strategy_console_frontend(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [node, str(Path(__file__).with_name("strategy_console_frontend.cjs")),
         str(root / "web/v2_strategy/app.js"), scenario],
        check=True, timeout=15,
    )
