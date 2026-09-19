"""R4B-ACK-PROPAGATION architecture guards."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "position_state/lifecycle_ack.py"
MODEL = ROOT / "docs/v2/P10_R4B_ACK_PROPAGATION_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_commit_precedes_and_guards_the_only_continuation_call():
    source = SOURCE.read_text()
    commit = source.index("self._commit_service.commit(")
    block = source.index("if acknowledgement.blocks_lifecycle:")
    continuation = source.index("continuation(acknowledgement)")
    assert commit < block < continuation
    assert source.count("continuation(acknowledgement)") == 1


def test_boundary_has_no_retry_io_or_exception_swallowing():
    source = SOURCE.read_text()
    service = source.split("class AcknowledgedSnapshotLifecycleService:", 1)[1]
    for forbidden in (
        "while ",
        "sleep(",
        "except ",
        "redis",
        "requests",
        "position_runtime",
    ):
        assert forbidden not in service


def test_boundary_remains_dormant_and_legacy_save_is_unchanged():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_state"):
            continue
        if "AcknowledgedSnapshotLifecycleService" in path.read_text():
            callers.append(str(relative))
    assert callers == []
    adapter = (ROOT / "execution/adapters/position_state.py").read_text()
    assert "def save_positions(self, positions: dict) -> None:" in adapter
    assert "R4B-ACK-PROPAGATION PASS" in MODEL.read_text()
    assert (
        "R4B-ACK-PROPAGATION Lifecycle Continuation Boundary | "
        "**IMPLEMENTED / CLOSED (DORMANT)**"
    ) in BACKLOG.read_text()
