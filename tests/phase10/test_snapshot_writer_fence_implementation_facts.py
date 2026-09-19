"""R4B-WRITER architecture guards for the dormant snapshot planner."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "docs/v2/P10_R4B_WRITER_FENCE_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_planner_is_pure_and_has_no_storage_dependency():
    source = (ROOT / "position_identity/snapshot_fence.py").read_text()
    for forbidden in (
        "redis",
        "requests",
        "shared.position_manager",
        "position_state",
        "pm:positions",
    ):
        assert forbidden not in source
    assert "def plan_legacy_snapshot_write(" in source


def test_every_batch_symbol_requires_explicit_canonical_reads():
    source = (ROOT / "position_identity/snapshot_fence.py").read_text()
    assert "symbols = set(current_snapshot) | set(proposed_snapshot)" in source
    assert "if set(canonical_reads) != symbols:" in source
    assert "SnapshotFenceReason.INCOMPLETE_READ_SET" in source


def test_active_is_preserved_flat_is_removed_and_errors_fail_closed():
    source = (ROOT / "position_identity/snapshot_fence.py").read_text()
    for token in (
        "AuthorityStatus.ACTIVE",
        "AuthorityStatus.QUARANTINED",
        "output[symbol] = deepcopy(current)",
        "AuthorityStatus.FLAT",
        "output.pop(symbol, None)",
        "SnapshotFenceCode.FAIL_CLOSED",
        "AUTHORITY_PROJECTION_MISMATCH",
        "CANONICAL_UNAVAILABLE",
        "CANONICAL_MALFORMED",
    ):
        assert token in source


def test_identity_match_includes_all_canonical_dimensions():
    source = (ROOT / "position_identity/snapshot_fence.py").read_text()
    for token in (
        "authority.exchange_position_key != projection.exchange_position_key",
        "authority.episode_id != projection.episode_id",
        "authority.slot_generation != projection.slot_generation",
        "authority.provenance is not projection.identity_provenance",
        "authority.legacy_position_id_alias",
        "!= projection.legacy_position_id_alias",
    ):
        assert token in source


def test_planner_remains_dormant_and_docs_preserve_active_gate():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_identity"):
            continue
        if "plan_legacy_snapshot_write" in path.read_text():
            callers.append(str(relative))
    assert callers == []
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert "not wired into `pm:positions` writes" in model
    assert "active R4B producer wiring remains `IN_PROGRESS`" in model
    assert "R4B-WRITER Legacy Snapshot Fence | **IMPLEMENTED / CLOSED (DORMANT)**" in backlog
    assert "R4 Active Producer Wiring | IN_PROGRESS" in backlog
