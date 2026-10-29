import shutil
import subprocess
from pathlib import Path

import pytest


def test_recovery_confirmation_retry_scope_races_and_safe_rendering():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for executable frontend QA")
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            node,
            str(root / "tests/v2_core/risk_recovery_frontend.cjs"),
            str(root / "web/v2_accounts/risk-recovery.js"),
        ],
        check=True,
        timeout=10,
    )
