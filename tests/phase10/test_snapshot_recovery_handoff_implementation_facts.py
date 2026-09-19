"""R4B-RECOVERY-HANDOFF architecture guards."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "position_state/recovery_handoff.py"
MODEL = ROOT / "docs/v2/P10_R4B_RECOVERY_HANDOFF_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_continuation_or_recovery_are_mutually_exclusive():
    source = SOURCE.read_text()
    continuation_gate = source.index("if lifecycle.continuation_completed:")
    recovery = source.index("self._recovery.handoff(")
    assert continuation_gate < recovery
    assert source.count("self._recovery.handoff(") == 1
    assert "recovery.acknowledged" in source


def test_contract_has_no_backend_runtime_retry_or_fallback():
    source = SOURCE.read_text()
    for forbidden in (
        "redis",
        "psycopg",
        "requests",
        "position_runtime",
        "while ",
        "sleep(",
        "except ",
    ):
        assert forbidden not in source.lower()


def test_handoff_remains_dormant_and_backend_decision_open():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_state"):
            continue
        if "RecoverableSnapshotLifecycleService" in path.read_text():
            callers.append(str(relative))
    assert callers == []
    model = MODEL.read_text()
    assert "product/operations approval" in model
    assert "R4B-RECOVERY-HANDOFF PASS" in model
    assert (
        "R4B-RECOVERY-HANDOFF Blocked Outcome Durable Port | "
        "**IMPLEMENTED / CLOSED (DORMANT)**"
    ) in BACKLOG.read_text()
