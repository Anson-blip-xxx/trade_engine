"""D2C-COORDINATOR guards for one-step verification orchestration."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "position_protection/verification_coordinator.py"
MODEL = ROOT / "docs/v2/P10_D2C_VERIFICATION_COORDINATOR_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_step_is_single_query_single_commit_and_non_blocking():
    source = SOURCE.read_text()
    step = source.split("    def step(", 1)[1].split("    def _transient", 1)[0]
    assert step.count("self._query.fetch(") == 1
    assert step.count("self._commit.commit(") == 1
    assert "while " not in source
    assert "sleep(" not in source


def test_ack_stale_unknown_and_quarantine_remain_distinct():
    source = SOURCE.read_text()
    for code in (
        "ACTIVE",
        "QUERY_AGAIN",
        "EXHAUSTED",
        "QUARANTINE",
        "RELOAD_CANONICAL",
        "RESOLVE_UNKNOWN",
    ):
        assert f'{code} = "{code}"' in source
    assert "if commit.applied:" in source
    assert "VerifiedActiveCommitCode.STALE" in source
    assert "VerifiedActiveCommitCode.UNKNOWN" in source


def test_coordinator_has_no_transport_or_runtime_dependency():
    source = SOURCE.read_text()
    for forbidden in (
        "requests",
        "redis",
        "_light_fapi_get",
        "shared.position_manager",
        "threading",
    ):
        assert forbidden not in source


def test_coordinator_remains_dormant_and_docs_preserve_transport_gate():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_protection"):
            continue
        if "ProtectionVerificationCoordinator" in path.read_text():
            callers.append(str(relative))
    assert callers == []
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert "no runtime caller" in model
    assert "reviewed signed transport" in model
    assert "R4B remains `IN_PROGRESS`" in model
    assert "D2C Verification Coordinator | **IMPLEMENTED / CLOSED (DORMANT)**" in backlog
