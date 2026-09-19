"""P10-07C architecture guards for dormant slot-authority storage."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'position_identity'
MODEL = ROOT / 'docs/v2/P10_D5A2_SLOT_AUTHORITY_IMPLEMENTATION.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _function(source, name, next_name):
    return source.split(f'def {name}', 1)[1].split(f'def {next_name}', 1)[0]


def test_slot_authority_key_is_dedicated_and_separate_from_positions():
    slot = (PACKAGE / 'slot.py').read_text()
    adapter = (PACKAGE / 'authority_redis.py').read_text()
    assert "STORAGE_KEY_PREFIX = 'pm:slot:v1'" in slot
    assert 'key.to_storage_key()' in adapter
    assert 'pm:positions' not in adapter


def test_authority_record_has_schema_version_and_required_fields():
    source = (PACKAGE / 'authority.py').read_text()
    tree = ast.parse(source)
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == 'SlotAuthority')
    fields = [node.target.id for node in cls.body
              if isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)]
    assert fields == [
        'exchange_position_key', 'episode_id', 'slot_generation', 'status',
        'provenance', 'revision', 'legacy_position_id_alias', 'created_at',
        'updated_at', 'schema_version',
    ]
    assert 'AUTHORITY_SCHEMA_VERSION = 1' in source


def test_provenance_values_are_fixed_and_records_are_frozen():
    source = (PACKAGE / 'authority.py').read_text()
    assert "NATIVE = 'NATIVE'" in source
    assert "MIGRATED = 'MIGRATED'" in source
    assert "RECONSTRUCTED = 'RECONSTRUCTED'" in source
    assert '@dataclass(frozen=True)\nclass SlotAuthority:' in source


def test_flat_transition_retains_generation_episode_and_provenance():
    source = (PACKAGE / 'authority.py').read_text()
    body = _function(source, 'transition_to_flat(', 'to_dict(')
    assert 'episode_id=self.episode_id' in body
    assert 'slot_generation=self.slot_generation' in body
    assert 'provenance=self.provenance' in body
    assert 'revision=self.revision + 1' in body


def test_lua_cas_checks_revision_status_episode_and_generation():
    source = (PACKAGE / 'authority_redis.py').read_text()
    for token in (
        "current['revision']", "current['status']",
        "current['episode_id']", "current['slot_generation']",
        'current_episode ~= cjson.null',
        "redis.call('SET', KEYS[1], new_json)",
    ):
        assert token in source
    assert 'compare_and_transition_slot' not in source or \
        'COMPARE_AND_TRANSITION_SLOT_LUA' in source


def test_authority_correctness_does_not_depend_on_locks_or_ttl():
    source = (PACKAGE / 'authority_redis.py').read_text()
    for token in ('lock_acquire', 'lock_renew', 'lock_release', 'PEXPIRE',
                  'EXPIRE', 'SETEX', "redis.call('DEL'"):
        assert token not in source


def test_authority_adapter_has_no_file_fallback_or_redis_import():
    source = (PACKAGE / 'authority_redis.py').read_text()
    tree = ast.parse(source)
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split('.')[0])
    assert 'redis' not in imported_roots
    for token in ('redis_store', 'Path(', 'open(', 'write_text', 'read_text',
                  'KEY_MAP', 'double_write'):
        assert token not in source


def test_authority_adapter_accepts_exchange_position_key_not_bare_slot_parts():
    source = (PACKAGE / 'authority_redis.py').read_text()
    assert 'key: ExchangePositionKey' in source
    assert "raise TypeError('key must be ExchangePositionKey')" in source
    for token in ('symbol: str', 'principal: str', 'environment: str'):
        assert token not in source


def test_only_protection_runtime_calls_authority_store():
    callers = []
    for path in ROOT.rglob('*.py'):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ('tests', 'position_identity'):
            continue
        source = path.read_text()
        if 'RedisSlotAuthorityAdapter' in source \
                or 'SlotAuthority' in source \
                or 'authority_redis' in source:
            callers.append(str(relative))
    assert set(callers) == {
        'position_protection/handoff.py',
        'position_protection/handoff_redis.py',
        'shared/position_manager.py',
    }


def test_protection_queue_identity_is_extended_but_authority_unwired():
    pm = (ROOT / 'shared/position_manager.py').read_text()
    enqueue = _function(pm, '_algo_enqueue(', '_algo_place_sl_inner(')
    worker_source = (ROOT / 'position_runtime/runtime.py').read_text()
    worker = _function(worker_source, 'algo_worker_loop(', 'start_algo_worker(')
    assert '_ALGO_QUEUE.append(task)' in enqueue
    assert 'parsed = classify_queue_task(task)' in worker
    for token in ('SlotAuthority', 'authority_redis'):
        assert token not in enqueue
        assert token not in worker


def test_legacy_position_payload_has_no_episode_authority_fields():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    cache = _function(source, '_update_pos_cache(', '_get_positions(')
    for token in ('episode_id', 'slot_generation', 'episode_status',
                  'identity_provenance', 'authority_revision'):
        assert token not in cache


def test_r4_principal_config_is_optional_and_explicit():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    assert "_ACCOUNT_PRINCIPAL_ID = ''" in source
    assert "k == 'ACCOUNT_PRINCIPAL_ID'" in source
    assert 'if _ACCOUNT_PRINCIPAL_ID:' in source
    for path in ('strategies/S6.py', 'strategies/S8.py'):
        assert 'ACCOUNT_PRINCIPAL_ID' not in (ROOT / path).read_text()


def test_authority_schema_contains_no_secret_or_wallet_material():
    source = (PACKAGE / 'authority.py').read_text()
    for token in ('api_key', 'api_secret', 'private_key', 'wallet',
                  'credential_fingerprint'):
        assert token not in source.lower()


def test_implementation_doc_and_backlog_close_d5a2_only():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    for phrase in (
        '`SlotAuthority`', 'pm:slot:v1:', '`schema_version = 1`',
        'slot_generation', 'revision', 'Lua', '`APPLIED`', '`CONFLICT`',
        '`BACKEND_ERROR`', '`RECONSTRUCTED`', '`QUARANTINED`',
        'ACTIVE RUNTIME BEHAVIOR = 0 CHANGE', 'P10-07D',
        '**P10-07C PASS.**',
    ):
        assert phrase in model
    assert 'P10-D5A-2 Slot Authority Store | **IMPLEMENTED / CLOSED**' \
        in backlog
    assert 'P10-D5A-3 Episode Onboarding | **IMPLEMENTED / CLOSED**' in backlog
