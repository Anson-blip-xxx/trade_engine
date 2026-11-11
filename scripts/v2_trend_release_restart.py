"""Guarded SANDBOX controller deployment with a separate explicit resume.

The first stage MUST hold MANAGE-only while the operator deploys compatible
policy readers and the v6 hub. Resume does not alter policy or bypass coverage.
No account identifiers or credentials are written to disk or printed.
"""

import argparse
import time
from uuid import uuid4

from scripts import v2_profitability_release_restart as protocol

PRIOR = protocol.ROOT / "releases/settlement-entry-witness-controller-20261029-v1"
CANDIDATE = protocol.ROOT / "releases/causal-trend-controller-20261030-v2"
IMPORT_PROGRAM = r"""
import importlib
from pathlib import Path
for name in (
    "services.v2_execution_supervisor", "services.v2_directional_cash",
    "services.v2_directional_replay", "services.v2_shared_risk_reference",
    "services.v2_jev_reference", "services.v2_s0_regime",
    "v2_core.trend_structure", "v2_core.trend_opportunities",
    "v2_core.capital_model", "v2_core.settlement_precision",
):
    source = Path(importlib.import_module(name).__file__)
    assert source.resolve().is_relative_to(Path.cwd().resolve())
    compile(source.read_bytes(), str(source), "exec")
from v2_core.runtime_policy import resolve
from services.v2_s3_candles import STRUCTURE_VERSION
from v2_core.directional import directional_events
assert "TREND_UP" in directional_events("S6")
assert "TREND_DOWN" in directional_events("S8")
assert STRUCTURE_VERSION == "s3-closed-1m-v6"
assert resolve()["entry.trend_structure_enabled"] is False
print("IMPORTS_AND_SAFE_DEFAULTS_VERIFIED")
"""


def resume(*, expected_epoch, apply=False):
    try:
        if not protocol.LINK.is_symlink() or protocol.LINK.resolve() != CANDIDATE:
            raise protocol.ReleaseError("TREND_CANDIDATE_REQUIRED")
        props = protocol.unit_properties()
        if props.get("ActiveState") != "active" or props.get("SubState") != "running":
            raise protocol.ReleaseError("RUNNING_CONTROLLER_REQUIRED")
        baseline = protocol.database("capture", capture_phase="DRAINING")
        if baseline["epoch"] != expected_epoch:
            raise protocol.ReleaseError("EXPECTED_ROUTE_EPOCH_REQUIRED")
        started = time.time_ns() // 1_000_000
        protocol.wait_route(baseline, phase="DRAINING", since_ms=started, timeout=60)
        if not apply:
            protocol.emit(
                "RESUME_PREFLIGHT_PASSED",
                epoch=expected_epoch,
                mutation_authorized=False,
            )
            return 0
        requested = protocol.database("request", baseline, request_id=str(uuid4()))
        if requested.get("epoch") != expected_epoch + 1:
            raise protocol.ReleaseError("UNEXPECTED_REQUEST_EPOCH")
        active = protocol.wait_route(
            baseline, phase="ACTIVE", since_ms=started, timeout=90
        )
        protocol.emit(
            "TREND_CONTROLLER_ACTIVE",
            epoch=active["epoch"],
            coverage=active["coverage"],
            same_binding=True,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - never surface private child details
        protocol.emit(
            "RESUME_BLOCKED",
            code=str(exc)
            if isinstance(exc, protocol.ReleaseError)
            else "UNEXPECTED_FAILURE",
        )
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--expected-epoch", type=int)
    args = parser.parse_args()
    protocol.PRIOR, protocol.CANDIDATE = PRIOR, CANDIDATE
    protocol.IMPORT_PROGRAM = IMPORT_PROGRAM
    if args.resume:
        if args.expected_epoch is None or args.expected_epoch < 0:
            parser.error("--resume requires --expected-epoch")
        return resume(expected_epoch=args.expected_epoch, apply=args.execute)
    return protocol.execute(apply=args.execute, resume=False)


if __name__ == "__main__":
    raise SystemExit(main())
