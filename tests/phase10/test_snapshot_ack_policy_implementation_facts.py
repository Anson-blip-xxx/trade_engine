"""R4B-ACK-POLICY architecture guards for typed caller decisions."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "position_state/snapshot_ack.py"
MODEL = ROOT / "docs/v2/P10_R4B_SNAPSHOT_ACK_POLICY_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_only_acknowledged_codes_advance_lifecycle():
    source = SOURCE.read_text()
    acknowledged = source.split("_ACKNOWLEDGED_CODES =", 1)[1].split(
        "def classify_snapshot_commit", 1
    )[0]
    for code in (
        "APPLIED",
        "ALREADY_APPLIED",
        "FILTERED_APPLIED",
        "FILTERED_ALREADY_APPLIED",
    ):
        assert f"FencedSnapshotCommitCode.{code}" in acknowledged
    for code in ("STALE", "UNAVAILABLE", "UNKNOWN", "INVALID"):
        assert f"FencedSnapshotCommitCode.{code}" not in acknowledged
    assert "return self.action is SnapshotCommitAction.ADVANCE" in source


def test_stale_unknown_and_unavailable_have_distinct_actions():
    source = SOURCE.read_text()
    assert "FencedSnapshotCommitCode.STALE" in source
    assert "SnapshotCommitAction.RELOAD_AND_REPLAN" in source
    assert "FencedSnapshotCommitCode.UNAVAILABLE" in source
    assert "SnapshotCommitAction.RETRY_WHEN_AVAILABLE" in source
    assert "FencedSnapshotCommitCode.UNKNOWN" in source
    assert "SnapshotCommitAction.RESOLVE_UNKNOWN" in source


def test_service_has_no_implicit_retry_or_exception_swallowing():
    source = SOURCE.read_text()
    service = source.split("class FencedSnapshotCommitService:", 1)[1]
    assert service.count("self._port.commit(") == 1
    assert "while " not in service
    assert "except " not in service


def test_policy_remains_dormant_and_docs_preserve_runtime_gate():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_state"):
            continue
        if "FencedSnapshotCommitService" in path.read_text():
            callers.append(str(relative))
    assert callers == []
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert "The service is dormant and has no runtime caller" in model
    assert "Active R4B migration\ntherefore remains `IN_PROGRESS`" in model
    assert "R4B-ACK-POLICY Typed Snapshot Caller Decisions | **IMPLEMENTED / CLOSED (DORMANT)**" in backlog
