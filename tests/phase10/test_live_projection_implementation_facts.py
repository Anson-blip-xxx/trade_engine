"""R1 architecture guards for dormant canonical live projection."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'position_identity'
MODEL = ROOT / 'docs/v2/P10_R1_CANONICAL_LIVE_PROJECTION_IMPLEMENTATION.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _fields(path, class_name):
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    return [node.target.id for node in cls.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)]


def test_projection_record_is_frozen_versioned_and_exact():
    source = (PACKAGE / 'projection.py').read_text()
    assert '@dataclass(frozen=True)\nclass LivePositionProjection:' in source
    assert 'PROJECTION_SCHEMA_VERSION = 1' in source
    assert _fields(PACKAGE / 'projection.py', 'LivePositionProjection') == [
        'exchange_position_key', 'episode_id', 'slot_generation',
        'identity_provenance', 'state_revision', 'last_operation_id',
        'side', 'system', 'quantity', 'entry_price', 'opened_at',
        'updated_at', 'legacy_position_id_alias', 'schema_version',
    ]


def test_projection_namespace_is_per_slot_and_never_legacy_snapshot():
    source = (PACKAGE / 'projection_redis.py').read_text()
    assert "STORAGE_KEY_PREFIX = 'pm:position-projection:v1'" in source
    assert 'canonical_digest()' in source
    assert 'pm:positions' not in source


def test_lua_has_complete_cas_fences_and_no_expiry_or_delete():
    source = (PACKAGE / 'projection_redis.py').read_text()
    for token in (
        "current['episode_id']", "current['slot_generation']",
        "current['state_revision']", "current['last_operation_id']",
        "proposed['state_revision'] ~= current['state_revision'] + 1",
        "redis.call('SET', KEYS[1], new_json)", 'field_count(record) ~= 14',
        'same_slot(', "return {'STALE', raw}",
    ):
        assert token in source
    for token in ('PEXPIRE', 'EXPIRE', 'SETEX', "redis.call('DEL'",
                  'lock_acquire', 'lock_renew'):
        assert token not in source


def test_projection_adapter_has_no_file_or_client_fallback():
    source = (PACKAGE / 'projection_redis.py').read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split('.')[0])
    assert 'redis' not in imported
    for token in ('Path(', 'open(', 'write_text', 'read_text',
                  'redis_store', 'double_write'):
        assert token not in source


def test_legacy_compatibility_is_controlled_and_fail_closed():
    source = (PACKAGE / 'projection_migration.py').read_text()
    for token in (
        'AuthorityStatus.ACTIVE', 'AuthorityProvenance.MIGRATED',
        'AUTHORITY_NOT_ADOPTED', 'MIXED_VERSION',
        'BLOCK_CANONICAL_PRESENT', 'FAIL_CLOSED',
        '_CANONICAL_FIELDS.intersection(legacy_row)',
    ):
        assert token in source
    for forbidden in ('redis', 'pm:positions', 'apply_legacy_adoption('):
        assert forbidden not in source


def test_only_r4_handoff_and_close_boundary_import_projection_implementation():
    callers = []
    needles = (
        'RedisLivePositionProjectionAdapter',
        'position_identity.projection',
        'prepare_legacy_projection',
        'legacy_snapshot_write_decision',
    )
    for path in ROOT.rglob('*.py'):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ('tests', 'position_identity'):
            continue
        source = path.read_text()
        if any(needle in source for needle in needles):
            callers.append(str(relative))
    assert set(callers) == {
        'position_protection/handoff.py',
        'position_protection/handoff_redis.py',
        'position_state/fenced_snapshot.py',
        'shared/position_manager.py',
    }


def test_active_legacy_writer_has_no_canonical_fields():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    body = source.split('def _update_pos_cache(', 1)[1].split(
        'def _get_positions(', 1)[0]
    for token in (
        'episode_id', 'slot_generation', 'identity_provenance',
        'state_revision', 'last_operation_id',
    ):
        assert token not in body


def test_doc_and_backlog_close_r1_without_claiming_r2_or_wiring():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    for phrase in (
        'IMPLEMENTED / CLOSED as dormant V2 infrastructure',
        'ACTIVE RUNTIME BEHAVIOR = 0 CHANGE',
        'pm:position-projection:v1:', '`STALE`', '`UNAVAILABLE`',
        '`UNKNOWN`', 'mixed-version writer guard', 'P10 R1 PASS.',
    ):
        assert phrase in model
    assert 'R1 Canonical Live Projection | **IMPLEMENTED / CLOSED**' in backlog
    assert 'P10-D3D-1D | episode/generation/revision conditional' in backlog
    assert 'IMPLEMENTED / CLOSED through R3' in backlog
