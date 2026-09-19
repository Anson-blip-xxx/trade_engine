"""P10-02 guards for current protection-establishment architecture facts."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / 'docs/v2/P10_D2_PROTECTION_ESTABLISHMENT_MODEL.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _function(source, name, next_name):
    return source.split(f'def {name}', 1)[1].split(f'def {next_name}', 1)[0]


def test_active_open_publishes_state_and_external_io_before_enqueue():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    body = _function(source, 'open_position(', '_round_qty(')
    assert body.index('_update_pos_cache(') < body.index('_pg_record_event(')
    assert body.index('_pg_record_event(') < body.index('tg_fn(msg)')
    assert body.index('tg_fn(msg)') < body.index('_algo_enqueue(')
    assert body.index('_algo_enqueue(') < body.index('return True')


def test_queue_is_unbounded_process_memory_with_identity():
    source = (ROOT / 'shared/position_manager.py').read_text()
    enqueue = _function(source, '_algo_enqueue(', '_algo_place_sl_inner(')
    assert '_ALGO_QUEUE = []' in source
    assert '_ALGO_QUEUE.append(task)' in enqueue
    assert 'maxsize' not in enqueue and 'len(_ALGO_QUEUE)' not in enqueue
    for token in ('position_id', 'request_id', '_rset'):
        assert token not in enqueue


def test_worker_pacing_is_after_attempt_and_has_no_retry_or_health():
    source = (ROOT / 'position_runtime/runtime.py').read_text()
    worker = _function(source, 'algo_worker_loop(', 'start_algo_worker(')
    tree = ast.parse('def algo_worker_loop(' + worker)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    names = [call.func.id for call in calls if isinstance(call.func, ast.Name)]
    assert names.count('place_fn') == 1
    assert worker.index('place_fn(') < worker.rindex('time.sleep(11)')
    assert 'queue.pop(0)' in worker and 'time.sleep(1)' in worker
    assert 'queue.append' not in worker
    for token in ('heartbeat', 'is_alive'):
        assert token not in worker.lower()


def test_place_ack_is_algo_id_only_without_post_ack_verification():
    source = (ROOT / 'shared/position_manager.py').read_text()
    place = _function(source, '_algo_place_sl_inner(', '_algo_cancel(')
    after_post = place.split("_light_fapi_post('/fapi/v1/algoOrder'", 1)[1]
    assert "isinstance(result, dict) and 'algoId' in result" in after_post
    assert '_light_fapi_get' not in after_post
    assert 'allAlgoOrders' not in after_post
    assert 'openAlgoOrders' not in after_post


def test_cancel_then_create_is_not_an_atomic_replace():
    source = (ROOT / 'shared/position_manager.py').read_text()
    place = _function(source, '_algo_place_sl_inner(', '_algo_cancel(')
    assert place.index('_cancel_all_algo(symbol)') < place.index(
        "_light_fapi_post('/fapi/v1/algoOrder'")
    cancel = _function(source, '_cancel_all_algo(', '_s6api(')
    assert "('NEW', 'WORKING', 'TRIGGERED')" in cancel
    assert 'FINISHED' not in cancel and 'EXPIRED' not in cancel


def test_full_close_does_not_invalidate_pending_queue_work():
    source = (ROOT / 'position_lifecycle/service.py').read_text()
    close = _function(source, 'close(', 'partial_close(')
    assert 'self.protection.cxa(symbol)' in close
    assert '_ALGO_QUEUE' not in close
    assert 'generation' not in close
    assert 'dequeue' not in close and 'purge' not in close


def test_no_restart_replay_or_worker_health_admission_gate():
    executor = (ROOT / 'strategies/shared_executor.py').read_text()
    open_body = _function(executor, 'open_position(', '_round_qty(')
    reconcile = _function(executor, 'reconcile_positions(', '_refresh_positions(')
    for token in ('_ALGO_WORKER_STARTED', 'is_alive', 'queue_depth',
                  'protection_health'):
        assert token not in open_body
    assert 'return state' in reconcile
    assert 'protection' not in reconcile.lower()
    assert 'replay' not in reconcile.lower()


def test_d2_document_and_backlog_record_blocked_readiness():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    for phrase in (
        'UNBOUNDED FILL-TO-PROTECTION GAP',
        'PROTECTED means that an exchange observation proves',
        'protection_generation',
        '**BLOCKED_BY_PRODUCT_DECISION**',
        '**P10-02 PASS.**',
    ):
        assert phrase in model
    assert 'P10-D2 Protection Establishment Model | **DESIGN AUDITED**' \
        in backlog
