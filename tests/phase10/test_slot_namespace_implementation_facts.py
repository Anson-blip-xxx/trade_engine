"""P10-07B architecture guards for dormant slot-identity infrastructure."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'position_identity'
MODEL = ROOT / 'docs/v2/P10_D5A1_SLOT_NAMESPACE_IMPLEMENTATION.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _function(source, name, next_name):
    return source.split(f'def {name}', 1)[1].split(f'def {next_name}', 1)[0]


def test_leaf_identity_module_exists_and_is_stdlib_only():
    paths = (
        PACKAGE / '__init__.py',
        PACKAGE / 'principal.py',
        PACKAGE / 'slot.py',
    )
    assert all(path.exists() for path in paths)
    forbidden_roots = {
        'redis', 'requests', 'psycopg', 'shared', 'strategies', 'execution',
        'position_lifecycle', 'position_protection', 'position_reconcile',
        'position_state', 'position_runtime',
    }
    for path in paths:
        tree = ast.parse(path.read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module.split('.')[0])
        assert not forbidden_roots.intersection(imports)


def test_principal_resolver_has_no_credential_or_io_dependency():
    source = (PACKAGE / 'principal.py').read_text()
    assert "ACCOUNT_PRINCIPAL_CONFIG_KEY = 'ACCOUNT_PRINCIPAL_ID'" in source
    assert 'raise ValueError' in source
    for token in ('API_KEY', 'API_SECRET', 'PRIVATE_KEY', 'SECRET_KEY',
                  'hashlib', 'os.environ', 'Path(', 'open(', 'requests', 'redis'):
        assert token not in source


def test_exchange_position_key_has_only_canonical_slot_dimensions():
    source = (PACKAGE / 'slot.py').read_text()
    tree = ast.parse(source)
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef)
               and node.name == 'ExchangePositionKey')
    fields = [node.target.id for node in cls.body
              if isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)]
    assert fields == [
        'exchange', 'product', 'environment', 'account_principal_id',
        'position_mode', 'symbol', 'slot_side',
    ]
    for token in ('system:', 'position_id:', 'request_id:', 'api_key:'):
        assert token not in source


def test_serialization_is_deterministic_and_not_repr_or_runtime_hash():
    source = (PACKAGE / 'slot.py').read_text()
    canonical = _function(source, 'to_canonical_string(', 'canonical_digest(')
    digest = _function(source, 'canonical_digest(', 'to_storage_key(')
    storage = _function(source, 'to_storage_key(', '__hash__(')
    assert 'json.dumps(' in canonical
    assert 'sort_keys=True' in canonical
    assert "separators=(',', ':')" in canonical
    assert 'repr(' not in canonical
    assert 'hash(' not in digest and 'hash(' not in storage
    assert 'hashlib.sha256(' in digest


def test_only_protection_fence_modules_call_identity_package():
    callers = []
    for path in ROOT.rglob('*.py'):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ('tests', 'position_identity'):
            continue
        if 'position_identity' in path.read_text():
            callers.append(str(relative))
    assert set(callers) == {
        'operation_journal/model.py',
        'position_protection/binance_observation.py',
        'position_protection/binance_query.py',
        'position_protection/claim.py',
        'position_protection/desired.py',
        'position_protection/desired_redis.py',
        'position_protection/fence.py',
        'position_protection/handoff.py',
        'position_protection/handoff_redis.py',
        'position_protection/migrated_plan.py',
        'position_protection/task.py',
        'position_protection/verification.py',
        'position_protection/verification_commit.py',
        'position_protection/verification_coordinator.py',
        'position_state/fenced_snapshot.py',
        'position_state/lifecycle_ack.py',
        'position_state/recovery_handoff.py',
        'position_state/snapshot_ack.py',
        'shared/position_manager.py',
    }


def test_no_slot_authority_redis_key_or_adapter_exists_yet():
    redis_store = (ROOT / 'shared/redis_store.py').read_text()
    state = (ROOT / 'execution/adapters/position_state.py').read_text()
    assert "'pm:positions'" in redis_store
    for token in ('pm:slot:v1', 'slot_authority',
                  'compare_and_transition_slot'):
        assert token not in redis_store
        assert token not in state


def test_active_position_schema_still_has_no_episode_fields():
    executor = (ROOT / 'strategies/shared_executor.py').read_text()
    cache = _function(executor, '_update_pos_cache(', '_get_positions(')
    assert "'position_id': position_id" in cache
    for token in ('episode_id', 'slot_generation', 'episode_status',
                  'identity_provenance', 'exchange_position_key'):
        assert token not in cache


def test_protection_queue_schema_carries_canonical_identity():
    source = (ROOT / 'shared/position_manager.py').read_text()
    enqueue = _function(source, '_algo_enqueue(', '_algo_place_sl_inner(')
    assert '_ALGO_QUEUE.append(task)' in enqueue
    for token in ('exchange_position_key', 'episode_id', 'slot_generation',
                  'protection_generation'):
        assert token in enqueue


def test_worker_parser_delegates_to_fenced_executor():
    source = (ROOT / 'position_runtime/runtime.py').read_text()
    worker = _function(source, 'algo_worker_loop(', 'start_algo_worker(')
    assert 'parsed = classify_queue_task(task)' in worker
    assert 'fenced_task = parsed.task' in worker
    assert 'execute_fn(fenced_task)' in worker
    for token in ('get_slot_authority', 'RedisSlotAuthorityAdapter',
                  'account_principal', 'can_mutate_async'):
        assert token not in worker


def test_r4_native_open_uses_explicit_optional_principal():
    executor = (ROOT / 'strategies/shared_executor.py').read_text()
    assert "_ACCOUNT_PRINCIPAL_ID = ''" in executor
    assert "k == 'ACCOUNT_PRINCIPAL_ID'" in executor
    assert 'if _ACCOUNT_PRINCIPAL_ID:' in executor
    assert '_algo_enqueue_native_open(' in executor
    pm = (ROOT / 'shared/position_manager.py').read_text()
    assert 'ExchangePositionKey.one_way(' in pm


def test_implementation_doc_and_backlog_close_d5a1_only():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    for phrase in (
        'ACTIVE RUNTIME BEHAVIOR = 0 CHANGE',
        '`AccountPrincipal`', '`ExchangePositionKey`',
        'PROD', 'DEMO', 'SANDBOX', 'ONE_WAY', 'BOTH',
        'to_canonical_string', 'to_storage_key',
        'position_identity', 'P10-07C', '**P10-07B PASS.**',
    ):
        assert phrase in model
    assert 'P10-D5A-1 Slot Namespace | **IMPLEMENTED / CLOSED**' in backlog
    assert 'P10-07C / D5A-2' in backlog
    assert 'READY_FOR_IMPLEMENTATION' in backlog
