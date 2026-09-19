"""R4B-IDENTITY architecture guards for dormant controlled migration."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "docs/v2/P10_R4B_IDENTITY_HANDOFF_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_migration_handoff_is_one_two_key_atomic_script():
    source = (ROOT / "position_identity/migration_handoff.py").read_text()
    assert "LEGACY_MIGRATION_HANDOFF_LUA" in source
    assert "LEGACY_MIGRATION_HANDOFF_LUA,\n                2," in source
    assert "redis.call('SET', KEYS[1], ARGV[4])" in source
    assert "redis.call('SET', KEYS[2], ARGV[5])" in source
    assert "if not expected(authority" in source
    assert "or not expected(projection" in source


def test_handoff_reuses_controlled_adoption_and_projection_validation():
    source = (ROOT / "position_identity/migration_handoff.py").read_text()
    assert "prepare_legacy_adoption(" in source
    assert "prepare_legacy_projection(" in source
    assert "AuthorityProvenance.MIGRATED" in source
    assert "AuthorityStatus.ACTIVE" in source
    assert "AuthorityProvenance.RECONSTRUCTED" not in source


def test_missing_or_unsafe_evidence_never_reaches_redis_eval():
    source = (ROOT / "position_identity/migration_handoff.py").read_text()
    eval_index = source.index("response = self._redis_eval(")
    for token in (
        "AdoptionClassification.LEGACY_QUARANTINE",
        "AdoptionClassification.NATIVE_AUTHORIZED",
        "AdoptionClassification.CONFLICT",
        "AdoptionClassification.INVALID",
        "LegacyProjectionCode.QUARANTINE",
    ):
        assert source.index(token) < eval_index


def test_handoff_remains_dormant_outside_identity_package():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_identity"):
            continue
        if "RedisLegacyMigrationHandoffAdapter" in path.read_text():
            callers.append(str(relative))
    assert callers == []


def test_docs_do_not_claim_active_r4b_or_deployability():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert "dormant isolated-V2 infrastructure" in model
    assert "No startup/runtime caller is wired" in model
    assert "R4B active producer remains `IN_PROGRESS`" in model
    assert "R4B-IDENTITY Legacy Identity Handoff | **IMPLEMENTED / CLOSED (DORMANT)**" in backlog
    assert "R4 Active Producer Wiring | IN_PROGRESS" in backlog
