"""P10-06B guards for current episode-authority architecture facts."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / 'docs/v2/P10_D3D1_EPISODE_FENCE_AUTHORITY.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _function(source, name, next_name):
    return source.split(f'def {name}', 1)[1].split(f'def {next_name}', 1)[0]


def test_current_state_has_no_canonical_episode_authority_field():
    active = (ROOT / 'strategies/shared_executor.py').read_text()
    cache = _function(active, '_update_pos_cache(', '_get_positions(')
    lifecycle = (ROOT / 'position_lifecycle/service.py').read_text()
    legacy = _function(lifecycle, 'open_position(', 'close_position(')
    assert "'position_id': position_id" in cache
    assert "'position_id'" not in legacy
    for body in (cache, legacy):
        for token in ('episode_id', 'position_episode_id', 'episode_status',
                      'identity_provenance'):
            assert token not in body


def test_current_position_id_is_legacy_time_and_system_coupled():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    cache = _function(source, '_update_pos_cache(', '_get_positions(')
    assert 'now = time.time()' in cache
    assert "position_id = f'{name}:{symbol}:{entry:.12g}:{now:.6f}'" \
        in cache
    assert cache.index('now = time.time()') < cache.index(
        "'position_id': position_id")


def test_active_reopen_id_formula_changes_with_local_creation_time():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    cache = _function(source, '_update_pos_cache(', '_get_positions(')
    assert "f'{name}:{symbol}:{entry:.12g}:{now:.6f}'" in cache
    first = f'S6:BTCUSDT:{100.0:.12g}:{1.0:.6f}'
    reopened = f'S6:BTCUSDT:{100.0:.12g}:{2.0:.6f}'
    assert first != reopened


def test_state_has_no_slot_or_protection_generation():
    paths = (
        'strategies/shared_executor.py',
        'position_state/service.py',
        'execution/adapters/position_state.py',
        'position_lifecycle/service.py',
    )
    for path in paths:
        source = (ROOT / path).read_text()
        assert 'slot_generation' not in source
        assert 'protection_generation' not in source


def test_position_store_is_symbol_only_without_account_environment_namespace():
    adapter = (ROOT / 'execution/adapters/position_state.py').read_text()
    executor = (ROOT / 'strategies/shared_executor.py').read_text()
    cache = _function(executor, '_update_pos_cache(', '_get_positions(')
    assert "PM_POSITIONS_KEY = 'pm:positions'" in adapter
    assert '_POS_CACHE[symbol]' in cache
    assert 'meta[symbol]' in cache
    for token in ('account_principal', 'subaccount', 'environment',
                  'position_mode', 'slot_side'):
        assert token not in adapter
        assert token not in cache


def test_current_execution_contract_assumes_one_way_both_slot():
    core = (ROOT / 'execution/core.py').read_text()
    active = _function(core, 'se_open_intent(', 'pm_open_intent(')
    pm_open = _function(core, 'pm_open_intent(', 'close_intent(')
    close = _function(core, 'close_intent(', 'partial_close_intent(')
    partial = _function(core, 'partial_close_intent(', '__missing_next_function__(')
    assert 'positionSide=' not in active
    for body in (pm_open, close, partial):
        assert "positionSide='BOTH'" in body
    state = (ROOT / 'position_state/service.py').read_text()
    parser = _function(state, 'parse_position_risk(', '__missing_next_function__(')
    assert "out[p['symbol']]" in parser
    assert 'positionSide' not in parser


def test_queue_carries_slot_episode_and_generation_identity():
    source = (ROOT / 'shared/position_manager.py').read_text()
    enqueue = _function(source, '_algo_enqueue(', '_algo_place_sl_inner(')
    assert '_ALGO_QUEUE.append(task)' in enqueue
    for token in ('exchange_position_key', 'episode_id', 'slot_generation',
                  'protection_generation'):
        assert token in enqueue


def test_worker_delegates_identity_and_pm_enforces_v1_v2():
    runtime = (ROOT / 'position_runtime/runtime.py').read_text()
    worker = _function(runtime, 'algo_worker_loop(', 'start_algo_worker(')
    assert 'parsed = classify_queue_task(task)' in worker
    assert 'execute_fn(fenced_task)' in worker
    for token in ('get_slot_authority', 'RedisSlotAuthorityAdapter',
                  'can_mutate_async'):
        assert token not in worker

    pm = (ROOT / 'shared/position_manager.py').read_text()
    execute = _function(pm, '_algo_execute_fenced_task(', '_algo_enqueue(')
    place = _function(pm, '_algo_place_sl_inner(', '_algo_cancel(')
    assert execute.index('fence.acquire(task)') < execute.index(
        '_algo_place_sl_inner(')
    assert place.index('_cancel_all_algo(symbol)') < place.index(
        'before_create()') < place.index(
        "_light_fapi_post('/fapi/v1/algoOrder'")


def test_algo_alias_writeback_is_symbol_only_without_cas():
    source = (ROOT / 'shared/position_manager.py').read_text()
    place = _function(source, '_algo_place_sl_inner(', '_algo_cancel(')
    assert "if symbol in positions:" in place
    assert "positions[symbol]['algo_sl_id'] = result['algoId']" in place
    assert '_save(positions)' in place
    for token in ('compare_and_swap', 'expected_episode',
                  'expected_generation', 'state_revision'):
        assert token not in place


def test_legacy_migration_has_no_authority_or_provenance():
    source = (ROOT / 'position_reconcile/service.py').read_text()
    migrate = _function(source, 'migrate_existing_positions(',
                        '__missing_next_function__(')
    assert "pos['system']" in migrate
    assert "pos['original_qty']" in migrate
    for token in ('episode_id', 'slot_generation', 'protection_generation',
                  'identity_provenance', 'schema_version', 'legacy_position_id'):
        assert token not in migrate


def test_exchange_snapshot_cannot_recover_native_episode():
    source = (ROOT / 'position_state/service.py').read_text()
    parser = _function(source, 'parse_position_risk(', '__missing_next_function__(')
    assert "'entry': float(p['entryPrice'])" in parser
    assert "'qty': amt" in parser
    for token in ('episode_id', 'position_id', 'slot_generation',
                  'request_id', 'orderId', 'fill_id', 'provenance'):
        assert token not in parser


def test_no_slot_generation_owner_or_cas_api_exists():
    state = (ROOT / 'position_state/service.py').read_text()
    adapter = (ROOT / 'execution/adapters/position_state.py').read_text()
    save = _function(state, 'save(', 'mark_closed(')
    tree = ast.parse('def save(' + save)
    args = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)).args.args
    assert [arg.arg for arg in args] == ['self', 'positions']
    combined = state + adapter
    for token in ('slot_generation', 'current_episode_id', 'fencing_token',
                  'compare_and_swap', 'expected_revision'):
        assert token not in combined


def test_episode_authority_document_and_backlog_readiness():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    for phrase in (
        'account_principal_id', 'position_mode', 'slot_side',
        'TEMPORARY_FENCE_ONLY', 'opaque UUID + slot generation',
        'exchange-confirmed flat', 'identity_provenance',
        'LEGACY_UNFENCED = DROP', 'NO CANCEL', 'NO CREATE', 'NO WRITEBACK',
        'D3D-1 = BLOCKED_BY_D5A', 'D3D-1A', 'D3D-1E',
        '**P10-06B PASS.**',
    ):
        assert phrase in model
    assert 'P10-D3D-1 Episode Fence Authority | **DESIGN AUDITED**' \
        in backlog
    assert '| P10-D3D-1 | protection queue episode fence' in backlog
    assert '| P10-D3D-1B | immutable queue identity extension' in backlog
    assert 'BLOCKED_BY_D2A' in backlog
