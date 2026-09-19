"""R4B-PROTECTION-COMMIT guards for the verified ACTIVE CAS."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "position_protection/verification_commit.py"
MODEL = ROOT / "docs/v2/P10_R4B_VERIFIED_ACTIVE_COMMIT_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_lua_compares_all_canonical_tokens_and_only_writes_desired():
    source = SOURCE.read_text()
    assert "authority ~= ARGV[1]" in source
    assert "projection ~= ARGV[2]" in source
    assert "desired ~= ARGV[3]" in source
    assert "redis.call('SET', KEYS[3], ARGV[4])" in source
    assert source.count("redis.call('SET'") == 1


def test_verification_precedes_the_atomic_commit():
    source = SOURCE.read_text()
    verify_at = source.index("verification = verify_current_protection(")
    reject_at = source.index("if not verification.verified:")
    eval_at = source.index("response = self._redis_eval(")
    assert verify_at < reject_at < eval_at
    assert "VerifiedActiveCommitCode.NOT_VERIFIED" in source


def test_unknown_recovery_is_bound_to_same_operation():
    source = SOURCE.read_text()
    assert "desired.status is ProtectionStatus.ACTIVE" in source
    assert "desired.last_operation_id == operation_id" in source
    assert "same_operation_recovery" in source
    assert "VerifiedActiveCommitCode.UNKNOWN" in source


def test_adapter_remains_dormant_and_docs_preserve_runtime_gate():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_protection"):
            continue
        if "RedisVerifiedActiveCommitAdapter" in path.read_text():
            callers.append(str(relative))
    assert callers == []
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert "No runtime\n> caller is wired" in model
    assert "R4B remains `IN_PROGRESS`" in model
    assert "R4B-PROTECTION-COMMIT Verified ACTIVE CAS | **IMPLEMENTED / CLOSED (DORMANT)**" in backlog
