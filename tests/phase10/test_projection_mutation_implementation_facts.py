"""R4B-MUTATION guards for authority-fenced projection reduction."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "docs/v2/P10_R4B_PROJECTION_MUTATION_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_mutation_is_one_two_key_authority_projection_script():
    source = (ROOT / "position_identity/projection_mutation.py").read_text()
    assert "REDUCE_PROJECTION_QUANTITY_LUA" in source
    assert "REDUCE_PROJECTION_QUANTITY_LUA,\n                2," in source
    assert "if authority ~= ARGV[1] then" in source
    assert "if projection ~= ARGV[2] then" in source
    assert "redis.call('SET', KEYS[2], ARGV[3])" in source
    assert "redis.call('SET', KEYS[1]" not in source


def test_all_identity_revisions_are_required_before_mutation():
    source = (ROOT / "position_identity/projection_mutation.py").read_text()
    for token in (
        "expected_episode_id",
        "expected_slot_generation",
        "expected_authority_revision",
        "expected_projection_revision",
        "authority.status is AuthorityStatus.ACTIVE",
        "authority.provenance is projection.identity_provenance",
        "authority.legacy_position_id_alias",
        "== projection.legacy_position_id_alias",
    ):
        assert token in source


def test_only_strict_positive_reduction_can_reach_lua():
    source = (ROOT / "position_identity/projection_mutation.py").read_text()
    validation = source.index("if new_quantity >= projection.quantity:")
    proposal = source.index("proposed = projection.revise(")
    execution = source.index("response = self._redis_eval(")
    assert validation < proposal < execution
    assert "new_quantity must strictly reduce current quantity" in source


def test_unknown_retry_and_stale_are_distinct_typed_outcomes():
    source = (ROOT / "position_identity/projection_mutation.py").read_text()
    for code in (
        "ALREADY_APPLIED",
        "STALE",
        "NOT_FOUND",
        "MALFORMED",
        "UNAVAILABLE",
        "UNKNOWN",
        "INVALID",
    ):
        assert f'{code} = "{code}"' in source
    assert "ACK can be ambiguous" in source
    assert "def _is_applied_retry(" in source


def test_adapter_remains_dormant_and_docs_preserve_runtime_gate():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_identity"):
            continue
        if "RedisProjectionMutationAdapter" in path.read_text():
            callers.append(str(relative))
    assert callers == []
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert "No lifecycle" in model
    assert "caller is wired" in model
    assert "active lifecycle" in model
    assert "R4B-MUTATION Projection Reduction CAS | **IMPLEMENTED / CLOSED (DORMANT)**" in backlog
    assert "R4 Active Producer Wiring | IN_PROGRESS" in backlog
