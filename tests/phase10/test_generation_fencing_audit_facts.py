"""P10-06A guards for current stale-work and generation-fencing facts."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / 'docs/v2/P10_D3D_GENERATION_FENCING_AUDIT.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _function(source, name, next_name):
    return source.split(f'def {name}', 1)[1].split(f'def {next_name}', 1)[0]


def _method(source, class_name, method_name):
    tree = ast.parse(source)
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    return next(node for node in cls.body
                if isinstance(node, ast.FunctionDef) and node.name == method_name)


def test_queue_payload_lacks_episode_and_protection_generation():
    source = (ROOT / 'shared/position_manager.py').read_text()
    enqueue = _function(source, '_algo_enqueue(', '_algo_place_sl_inner(')
    assert '_ALGO_QUEUE.append((symbol, side, trigger_price, qty))' in enqueue
    for token in ('position_id', 'episode_id', 'episode_token',
                  'protection_generation', 'request_id'):
        assert token not in enqueue


def test_worker_pops_fifo_without_reloading_current_identity():
    source = (ROOT / 'position_runtime/runtime.py').read_text()
    worker = _function(source, 'algo_worker_loop(', 'start_algo_worker(')
    assert 'task = queue.pop(0)' in worker
    assert 'symbol, side, trigger_price, qty = task' in worker
    assert worker.index('queue.pop(0)') < worker.index('place_fn(')
    for token in ('load', 'position_id', 'episode', 'generation', 'current'):
        assert token not in worker


def test_place_does_not_compare_identity_before_cancel_or_create():
    source = (ROOT / 'shared/position_manager.py').read_text()
    place = _function(source, '_algo_place_sl_inner(', '_algo_cancel(')
    before_cancel = place.split('_cancel_all_algo(symbol)', 1)[0]
    assert '_load()' not in before_cancel
    assert place.index('_cancel_all_algo(symbol)') < place.index(
        "_light_fapi_post('/fapi/v1/algoOrder'")
    assert place.index("_light_fapi_post('/fapi/v1/algoOrder'") < \
        place.index('positions = _load()')
    for token in ('position_id', 'episode', 'generation'):
        assert token not in place


def test_cancel_all_uses_symbol_as_its_only_task_ownership_key():
    source = (ROOT / 'shared/position_manager.py').read_text()
    cancel = _function(source, '_cancel_all_algo(', '_s6api(')
    assert "{'symbol': symbol}" in cancel
    assert "('NEW', 'WORKING', 'TRIGGERED')" in cancel
    for token in ('position_id', 'episode', 'generation', 'expected'):
        assert token not in cancel


def test_full_close_removes_state_without_invalidating_memory_queue():
    source = (ROOT / 'position_lifecycle/service.py').read_text()
    close = _function(source, 'close(', 'partial_close(')
    assert 'positions.pop(symbol, None)' in close
    assert 'self.state.save(positions)' in close
    for token in ('_ALGO_QUEUE', 'dequeue', 'purge', 'generation'):
        assert token not in close


def test_active_open_has_position_id_before_enqueue_but_drops_it():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    body = _function(source, 'open_position(', '_round_qty(')
    assert body.index('position_id = _update_pos_cache(') < body.index(
        '_algo_enqueue(')
    enqueue_call = body[body.index('_algo_enqueue('):].split(')', 1)[0]
    assert 'position_id' not in enqueue_call


def test_legacy_open_enqueues_before_position_record_exists():
    source = (ROOT / 'position_lifecycle/service.py').read_text()
    body = _function(source, 'open_position(', 'close_position(')
    assert body.index('self.protection.enq(') < body.index('position = {')
    before_enqueue = body.split('self.protection.enq(', 1)[0]
    assert 'position_id' not in before_enqueue


def test_break_even_replace_has_no_generation_comparison():
    source = (ROOT / 'shared/position_manager.py').read_text()
    body = _function(source, '_update_stop_loss(', '_calc_atr(')
    assert body.index('_algo_cancel(old_algo)') < body.index('_algo_enqueue(')
    for token in ('position_id', 'episode', 'generation', 'expected'):
        assert token not in body


def test_trailing_replace_cancels_and_enqueues_without_generation():
    source = (ROOT / 'shared/position_manager.py').read_text()
    body = _function(source, '_place_trail_sl(', '_execution_service(')
    assert body.index('_save(positions)') < body.index(
        '_cancel_all_algo(symbol)')
    assert body.index('_cancel_all_algo(symbol)') < body.index(
        '_algo_enqueue(')
    for token in ('position_id', 'episode', 'generation', 'expected'):
        assert token not in body


def test_worker_has_no_stale_drop_log_or_metric():
    runtime = (ROOT / 'position_runtime/runtime.py').read_text()
    worker = _function(runtime, 'algo_worker_loop(', 'start_algo_worker(').lower()
    pm = (ROOT / 'shared/position_manager.py').read_text()
    place = _function(pm, '_algo_place_sl_inner(', '_algo_cancel(').lower()
    for token in ('stale', 'superseded', 'generation_mismatch',
                  'stale_task_count'):
        assert token not in worker
        assert token not in place


def test_position_records_have_no_generation_counter():
    active = (ROOT / 'strategies/shared_executor.py').read_text()
    cache = _function(active, '_update_pos_cache(', '_get_positions(')
    lifecycle = (ROOT / 'position_lifecycle/service.py').read_text()
    legacy_open = _function(lifecycle, 'open_position(', 'close_position(')
    assert "'position_id'" in cache
    assert "'position_id'" not in legacy_open
    for body in (cache, legacy_open):
        assert 'protection_generation' not in body
        assert 'episode_generation' not in body
        assert 'state_revision' not in body


def test_marker_clear_is_symbol_only_unconditional_delete():
    source = (ROOT / 'position_state/service.py').read_text()
    clear = _method(source, 'PositionStateService', 'clear_closed')
    assert [arg.arg for arg in clear.args.args] == ['self', 'symbol']
    body = ast.unparse(clear)
    assert 'self._marker_delete(self.MARKER_PREFIX + symbol)' in body
    for token in ('expected', 'episode', 'generation', 'token'):
        assert token not in body


def test_reconcile_uses_symbol_snapshots_without_version_fence():
    source = (ROOT / 'position_reconcile/service.py').read_text()
    body = _function(source, 'reconcile_all(', 'notify_external_position(')
    assert "real_positions[p['symbol']]" in body
    assert 'positions.pop(sym, None)' in body
    assert 'self.state.save(positions)' in body
    for token in ('position_id', 'generation', 'revision', 'compare'):
        assert token not in body


def test_d3d_document_and_backlog_record_audited_blocked_scope():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    for phrase in (
        'queued protection create', 'queued protection replace',
        'delayed marker clear', 'delayed close result',
        'delayed reconcile result', 'monitor callback',
        'ghost cleanup callback', 'position_id',
        'Protection Queue Episode Fence', 'STALE -> drop + log + metric',
        'BLOCKED_BY_D5_PRODUCT_DECISION', 'BLOCKED_BY_D2_PRODUCT_DECISION',
        '**P10-06A PASS.**',
    ):
        assert phrase in model
    assert 'P10-D3D Generation Fencing Audit | **DESIGN AUDITED**' in backlog
    assert '| P10-D3D-1 |' in backlog and '| P10-D3D-4 |' in backlog
