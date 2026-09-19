"""R2 architecture guards for dormant desired-protection authority."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'position_protection'
MODEL = ROOT / 'docs/v2/P10_R2_DESIRED_PROTECTION_IMPLEMENTATION.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _fields(path, class_name):
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    return [node.target.id for node in cls.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)]


def test_record_is_frozen_versioned_and_exact():
    source = (PACKAGE / 'desired.py').read_text()
    assert '@dataclass(frozen=True)\nclass DesiredProtectionRecord:' in source
    assert 'DESIRED_PROTECTION_SCHEMA_VERSION = 1' in source
    assert _fields(PACKAGE / 'desired.py', 'DesiredProtectionRecord') == [
        'exchange_position_key', 'episode_id', 'slot_generation',
        'protection_generation', 'desired_intent_id', 'trigger_price',
        'covered_quantity', 'closing_side', 'status',
        'exchange_algo_aliases', 'revision', 'last_operation_id',
        'created_at', 'updated_at', 'schema_version',
    ]


def test_redis_namespace_and_cas_use_all_required_fences():
    source = (PACKAGE / 'desired_redis.py').read_text()
    assert "STORAGE_KEY_PREFIX = 'pm:desired-protection:v1'" in source
    for token in (
        "current['episode_id']", "current['slot_generation']",
        "current['protection_generation']", "current['revision']",
        "current['last_operation_id']", 'same_spec(',
        'legal_transition(', "redis.call('SET', KEYS[1], new_json)",
    ):
        assert token in source
    assert 'canonical_digest()' in source


def test_generation_and_revision_are_separate():
    source = (PACKAGE / 'desired.py').read_text()
    next_desired = source.split('def next_desired(', 1)[1].split(
        'def transition(', 1)[0]
    transition = source.split('def transition(', 1)[1].split(
        'def to_dict(', 1)[0]
    assert 'protection_generation=self.protection_generation + 1' \
        in next_desired
    assert 'revision=self.revision + 1' in next_desired
    assert 'protection_generation=self.protection_generation' in transition
    assert 'revision=self.revision + 1' in transition


def test_adapter_has_no_ttl_delete_file_snapshot_or_client_fallback():
    source = (PACKAGE / 'desired_redis.py').read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split('.')[0])
    assert 'redis' not in imported
    for token in ('pm:positions', 'PEXPIRE', 'EXPIRE', 'SETEX',
                  "redis.call('DEL'", 'Path(', 'open(', 'write_text'):
        assert token not in source


def test_only_dormant_recovery_fence_imports_desired_outside_protection():
    callers = []
    needles = (
        'RedisDesiredProtectionAdapter',
        'position_protection.desired',
        'DesiredProtectionRecord',
    )
    for path in ROOT.rglob('*.py'):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ('tests', 'position_protection'):
            continue
        source = path.read_text()
        if any(needle in source for needle in needles):
            callers.append(str(relative))
    assert set(callers) == {
        'operation_journal/generation_fence.py',
        'operation_journal/generation_reader.py',
    }


def test_active_queue_still_has_no_desired_generation_or_durable_handoff():
    source = (ROOT / 'shared/position_manager.py').read_text()
    enqueue = source.split('def _algo_enqueue(', 1)[1].split(
        'def _algo_place_sl_inner(', 1)[0]
    for token in (
        'DesiredProtectionRecord', 'desired_intent_id',
        'RedisDesiredProtectionAdapter', 'pm:desired-protection:v1',
    ):
        assert token not in enqueue


def test_doc_never_claims_alias_or_ack_proves_protected():
    model = MODEL.read_text()
    for phrase in (
        'ACTIVE RUNTIME BEHAVIOR = 0 CHANGE',
        'algoId` values are recovery aliases',
        'R2 never describes an ACK',
        'transitions a record to `ACTIVE`',
        '**P10 R2 PASS.**',
    ):
        assert phrase in model


def test_backlog_closes_d2a_only_and_advances_d3d1d():
    backlog = BACKLOG.read_text()
    assert 'R2 / P10-D2A Desired Protection | **IMPLEMENTED / CLOSED**' \
        in backlog
    assert '| P10-D3D-1D | episode/generation/revision conditional' in backlog
    assert 'IMPLEMENTED / CLOSED through R3' in backlog
    for still_open in (
        'P10-D2B | durable handoff',
        'P10-D2C | exchange Algo ACK',
        'P10-D2D | generation-fenced reconcile',
        'P10-D2E | SLO, admission',
    ):
        assert still_open in backlog
